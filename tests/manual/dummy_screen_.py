"""Dummy screen: the real MicroTui with fake data. No network, HOME is a temp dir.

    source env/bin/activate
    python tests/manual/dummy_screen_.py

Slash commands run for real (/model /login /theme /rewind /dangerous /subagents, ctrl+b for bgprocs).
Dummy-only: /ask /ask-multi /ask-free /approve /approve-long /endpoint /ambient. Plain text gets a canned reply.
"""

import asyncio
import os
import sys
import tempfile
from pathlib import Path

_HOME = tempfile.mkdtemp(prefix="microcc-dummy-")
os.environ["HOME"] = _HOME  # before any micro_cc import: settings, .env and message store land here
os.environ["ANTHROPIC_API_KEY"] = "dummy"

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))

from micro_cc.start_live_tui_ import MicroTui  # noqa: E402
from micro_cc import mods_  # noqa: E402
from micro_cc.tools import bash_tool, monitor_watch_  # noqa: E402
from micro_cc.tui_native import etype_handler_  # noqa: E402
from micro_cc.tui_native import screen_cmds_ as sc  # noqa: E402
from micro_cc.tui_native.alt_screen_ import ENTER_ALT_SCREEN, EXIT_ALT_SCREEN  # noqa: E402
from micro_cc.tui_native.keys_ import parse_key  # noqa: E402
from micro_cc.tui_native.message_row_ import MessageRow  # noqa: E402
from micro_cc.utils import command_registry  # noqa: E402
from micro_cc.utils.msg_store_ import store_msgs  # noqa: E402

INTRO = (
    "**Dummy screen.** Nothing here touches your settings or the network.\n\n"
    "- `/model` `/login` `/theme` `/rewind` `/dangerous` `/subagents` work for real\n"
    "- `/ask` `/ask-multi` `/ask-free` `/approve` `/approve-long` open the question and approval panels\n"
    "- `/endpoint` cycles Anthropic / LiteLLM / Ollama so `/model` shows each list, `/ambient` toggles the status lines\n"
    "- `ctrl+b` opens the background processes, `@` and `/` open the live panels, ctrl+c or `/exit` quits"
)

MESSAGES = [
    {"type": "text", "content": INTRO},
    {"type": "user", "content": "explain how the scroll view follows the bottom"},
    {"type": "text", "content": "# heading\n\nSome **bold** and `inline code`, then a fence:\n\n```python\ndef f(x):\n    return x + 1\n```"},
    {"type": "thinking", "content": "weighing whether ScrollView or VStack owns this..."},
    {"type": "tool_call", "name": "bash_", "input": {"command": "git status"}, "result": "\x1b[32mM src/foo.py\x1b[0m\n\x1b[31mD src/bar.py\x1b[0m", "expanded": True},
    {"type": "tool_call", "name": "bash_", "input": {"command": "ls -la"}, "result": "total 12\ndrwxr-xr-x  a  b  c", "expanded": False},
    {"type": "error", "content": "No API endpoint configured — run /login"},
    {"type": "approval", "name": "bash_", "input": {"command": "rm -rf build/"}, "expanded": False},
    {"type": "user_queued", "content": "second question, queued mid-turn"},
]

ASK = [{"header": "db", "question": "Which database should the new service use for the event log?",
        "options": [{"label": "Postgres", "description": "Already in the stack, transactional, boring. Needs a migration for the new table and an index on (user_id, ts)."},
                    {"label": "SQLite", "description": "Zero ops, single file. Fine until two writers show up."},
                    {"label": "DuckDB", "description": "Great for the analytics queries, awkward for concurrent inserts."}]}]
ASK_MULTI = [{"header": "env", "question": "Which environments should this deploy to?", "multiSelect": True,
              "options": [{"label": "dev", "description": "auto-deploys on merge"}, {"label": "staging"}, {"label": "prod", "description": "needs approval"}]}]
ASK_FREE = [{"header": "name", "question": "What should the new branch be called?", "options": []}]
ENDPOINTS = {"Anthropic": {"ANTHROPIC_API_KEY": "dummy"}, "LiteLLM": {"LITELLM_BASE_URL": "x", "LITELLM_API_KEY": "x"}, "Ollama": {"OLLAMA_BASE_URL": "x"}}
_ENDPOINT_VARS = {k for env in ENDPOINTS.values() for k in env}

PROCS = [{"pid": 4821, "age": 312, "command": "npm run dev -- --host 0.0.0.0 --port 5173", "cwd": "/Users/george/dev/site"},
         {"pid": 5110, "age": 41, "command": "python -m http.server 8000", "cwd": None}]
WATCHES = [{"watch_id": "w1", "age": 90, "description": "tail the build log for ERROR", "command": "tail -f build.log | grep ERROR"}]
bash_tool.list_background_processes = lambda: PROCS
monitor_watch_.list_watches = lambda: WATCHES


def set_ambient(app, on: bool) -> None:
    app.working.update("⠋ Working…" if on else "")
    app.subagent_status.update("⚙ headless-worker-1: RUNNING (3 pending)\n⚙ headless-worker-2: DONE" if on else "")
    app.bgproc_status.update("◇ PID 4821: npm run dev  (312s)" if on else "")
    app.memory_flash.update("[italic]✎ memory reviewed — + prefers short docstrings; ~ updated picker notes[/italic]" if on else "")
    app._ambient = on


def add_commands(app) -> None:
    async def ask(qs):
        app.prompt.clear()
        answered = {"answered": None}
        await etype_handler_.handle_question_asked(app, {"answered": answered, "input": {"questions": qs}}, app.message_list, {}, {})
        app._input_mode = "idle"
        app._flash_status(f"answered: {answered['answered']}", seconds=6)

    async def approve(name, inp):
        app.prompt.clear()
        approval = {"approved": None}
        event = {"approval": approval, "id": os.urandom(3).hex(), "name": name, "input": inp}
        await etype_handler_.handle_approval_request(app, event, app.message_list, {}, {})
        app._input_mode = "idle"
        app._flash_status(f"approved: {approval['approved']}", seconds=6)

    async def endpoint(_app):
        names = list(ENDPOINTS)
        current = next((n for n in names if all(os.environ.get(k) for k in ENDPOINTS[n])), None)
        nxt = names[(names.index(current) + 1) % len(names)] if current in names else names[0]
        for k in _ENDPOINT_VARS:
            os.environ.pop(k, None)
        os.environ.update(ENDPOINTS[nxt])
        app._flash_status(f"endpoint: {nxt}")

    async def ambient(_app):
        set_ambient(app, not app._ambient)

    def disabled(_app):
        async def run(_a):
            app._flash_status("disabled in the dummy screen")
        return run

    extra = {
        "/ask": ("question panel, described options", lambda a: ask(ASK)),
        "/ask-multi": ("question panel, multi-select", lambda a: ask(ASK_MULTI)),
        "/ask-free": ("question, freeform answer", lambda a: ask(ASK_FREE)),
        "/approve": ("approval panel, short input", lambda a: approve("bash_", {"command": "git push origin main"})),
        "/approve-long": ("approval panel, long input", lambda a: approve("write_", {"file_path": "/Users/george/dev/micro-cc/src/micro_cc/tui_native/option_panel_.py", "content": "x = 1\n" * 80})),
        "/endpoint": ("cycle Anthropic / LiteLLM / Ollama", endpoint),
        "/ambient": ("toggle working / subagent / bgproc lines", ambient),
    }
    for name, (hint, fn) in extra.items():
        sc.SLASH_COMMANDS[name] = fn
        command_registry.COMMANDS.append({"name": name, "hint": hint, "kind": "ui"})
    for name in ("/update", "/reload", "/gui", "/clear", "/author", "/copy"):
        sc.SLASH_COMMANDS[name] = disabled(name)

    original = app._on_prompt_submitted

    async def submit(text, *args, **kwargs):
        query = text.strip()
        head = query.split()[0] if query else ""
        if app._input_mode == "idle" and query and (head in sc.PREFIX_COMMANDS or not query.startswith("/")):
            app.prompt.clear()
            await app._mount_row({"type": "user", "content": query})
            await app._mount_row({"type": "text", "content": "*(dummy reply — no model is called here)*"})
            return
        if app._input_mode == "idle" and head.startswith("/") and head not in sc.SLASH_COMMANDS and not mods_.has("command", name=head[1:]):
            app.prompt.clear()
            app._flash_status(f"unknown command {head}")
            return
        await original(text, *args, **kwargs)

    app._on_prompt_submitted = submit


def build() -> MicroTui:
    store_msgs(str(REPO), [
        m for i in range(1, 61) for m in (
            {"role": "user", "content": f"turn {i}: " + ("refactor the pickers so they share one renderer and keep the filter " * 3)[: 30 + (i * 7) % 70]},
            {"role": "assistant", "content": f"done {i}"},
        )
    ])
    app = MicroTui(str(REPO), messages=[])
    for msg in MESSAGES:
        app.message_list.add(MessageRow(msg))
    app._msg_rows = list(MESSAGES)
    app._project_files = [str(p.relative_to(REPO)) for p in sorted((REPO / "src").rglob("*.py"))][:400]
    app.statusbar.update("⏣ dummy | ◈ sonnet-5")
    set_ambient(app, True)
    add_commands(app)
    return app


async def amain() -> None:
    app = build()
    loop = asyncio.get_running_loop()
    app._running = True
    app._static_hint_text()
    app.set_focus(app.prompt)

    def on_input(data: str) -> None:
        if parse_key(data) == "ctrl+c":
            app._running = False
        else:
            app._on_terminal_input(data)

    sys.stdout.write(ENTER_ALT_SCREEN)
    sys.stdout.flush()
    app.terminal.start(on_input, app._on_terminal_resize)
    loop.add_reader(sys.stdin.fileno(), app._on_stdin_readable)
    render = asyncio.create_task(app._render_loop())
    app.request_render()
    try:
        while app._running:
            await asyncio.sleep(0.1)
    finally:
        render.cancel()
        loop.remove_reader(sys.stdin.fileno())
        app.terminal.stop()
        sys.stdout.write(EXIT_ALT_SCREEN)
        sys.stdout.flush()


if __name__ == "__main__":
    asyncio.run(amain())
