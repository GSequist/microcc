"""Native-renderer command handlers (same commands as screen_cmds_.py)."""

import os
import signal
import subprocess
import time

from micro_cc.cache import state_store
from micro_cc.claude_loop_ import GATEABLE_TOOLS
from micro_cc.utils.msg_store_ import (
    load_msgs,
    erase_msgs,
    erase_summary,
    erase_checkpoint,
)
from micro_cc.tools.browser_tool_ import close_browser
from micro_cc.utils.tokenization_simple import erase_token_stats
from micro_cc.utils.helpers import has_configured_endpoint
from micro_cc.utils import hidden_prompts, command_registry, settings_store_
from micro_cc.tui_native.list_picker_ import PickerItem
from micro_cc.tui_native.glyphs_ import glyph
from micro_cc.tui_native.detect_images_ import delete_all_kitty_images
import sys


async def cmd_clear(app):
    # Finalize streaming row BEFORE clearing message_list to prevent stray row re-addition.
    app._finalize_streaming()
    app.message_list.clear()
    app._msg_rows = []

    # /clear erases chat but not tracked_subagents (separate OS processes may still be running).
    # Reset UI view state only to avoid orphaning live processes.
    if app._subagent_viewing_target is not None:
        app._subagent_viewing_target = None
        app.root.replace(app.subagent_scroll_view, app.messages_scroll)
    if app._bgproc_viewing:
        app._hide_bgproc_detail()

    erase_msgs(app._project_dir)
    erase_summary(app._project_dir)
    erase_checkpoint(app._project_dir)
    state_store.clear_todos(app._project_dir)
    import shutil
    await close_browser()
    for ss_folder in (".browser_screenshots", ".computer_screenshots"):
        ss_dir = os.path.join(app._project_dir, ss_folder)
        if os.path.isdir(ss_dir):
            shutil.rmtree(ss_dir)
    # Kitty images are composited separately; must be cleared explicitly to avoid orphaned pixels.
    sys.stdout.write(delete_all_kitty_images())
    sys.stdout.flush()
    erase_token_stats(app._project_dir)
    app._flash_status("⇤ all messages erased")
    app.prompt.clear()


async def cmd_model(app):
    app._open_picker(app.model_picker, "_model_picker_overlay")
    app.prompt.clear()


async def cmd_theme(app):
    """Open the theme picker."""
    app._open_picker(app.theme_picker, "_theme_picker_overlay")
    app.prompt.clear()


async def cmd_copy(app):
    lines = []
    for msg in app._msg_rows:
        t = msg["type"]
        if t == "user":
            lines.append(f"{glyph('user_prompt')} {msg['content']}")
        elif t == "text":
            lines.append(msg["content"])
        elif t == "thinking":
            lines.append(f"[thinking] {msg['content']}")
        elif t == "tool_call":
            result = msg.get("result") or "⋯"
            lines.append(f"{glyph('tool')} {msg['name']} → {result}")
        elif t == "error":
            lines.append(f"{glyph('error')} {msg['content']}")
    if app._streaming is not None:
        content = app._streaming.get_content()
        if content.strip():
            lines.append(content)
    text = "\n\n".join(lines)
    app.copy_to_clipboard(text)
    app.prompt.clear()


async def cmd_dangerous(app):
    app.dangerous_picker.set_items([PickerItem(name, name) for name in GATEABLE_TOOLS])
    app.dangerous_picker.checked = set(app._dangerous_tools) & set(GATEABLE_TOOLS)
    app._open_picker(app.dangerous_picker, "_dangerous_picker_overlay")
    app.prompt.clear()


SUBAGENT_CAP_CHOICES = (1, 2, 3, 4, 5, 6, 8, 10, 12, 16)


async def cmd_subagents(app):
    """Pick the concurrent-subagent cap (settings "max_subagents")."""
    current = settings_store_.max_subagents()
    choices = sorted(set(SUBAGENT_CAP_CHOICES) | {current})
    default = settings_store_.DEFAULT_MAX_SUBAGENTS
    app.subagents_picker.set_items([
        PickerItem(str(n), f"{n} at once" + (" (default)" if n == default else "") + ("  ← current" if n == current else ""))
        for n in choices
    ])
    app.subagents_picker.selected_index = choices.index(current)
    app._open_picker(app.subagents_picker, "_subagents_picker_overlay")
    app.prompt.clear()


async def cmd_rewind(app):
    msgs = load_msgs(app._project_dir)
    options = app.get_rewind_options(msgs)
    # id carries JSONL index so duplicate-label turns stay distinct.
    app.rewind_picker.set_items([
        PickerItem(str(idx), f"{n:>3}. {label}") for n, (idx, label) in enumerate(options, 1)
    ])
    if options:
        app.rewind_picker.selected_index = len(options) - 1
    app._open_picker(app.rewind_picker, "_rewind_picker_overlay")
    app.prompt.clear()


async def cmd_login(app):
    app._input_mode = "login"
    app.login_picker.set_items([
        PickerItem(name, name) for name in ("Anthropic", "Bad Bunny", "Foundry", "LiteLLM", "OpenAI", "OpenRouter", "Ollama")
    ])
    app._open_picker(app.login_picker, "_login_picker_overlay")
    app.prompt.clear()


async def cmd_exit(app):
    app._running = False


async def cmd_update(app):
    app.prompt.clear()
    if not app._update_available:
        app._flash_status("✓ already on the latest version")
        return
    await _run_update(app)


def _pink() -> str:
    """Get accent color from active theme (function so /theme switch takes effect)."""
    from micro_cc.utils import theme_store_
    return theme_store_.get("accent")


async def _run_update(app):
    """Install update and re-exec with fresh module imports."""
    import asyncio
    import sys
    from importlib.metadata import version as _pkg_version

    latest = app._update_available
    current_version = _pkg_version("micro-cc")

    from micro_cc.self_heal_ import install_cmd

    proc = await asyncio.create_subprocess_exec(
        *install_cmd("micro-cc"),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )

    chunks = []

    async def _drain():
        async for line in proc.stdout:
            chunks.append(line)

    drain_task = asyncio.create_task(_drain())
    wait_task = asyncio.ensure_future(proc.wait())

    i = 0
    while True:
        frame = app._SPINNER[i % len(app._SPINNER)]
        app.static_hintbar.update(f"[{_pink()}]{frame} updating micro-cc {current_version} → {latest}…[/{_pink()}]")
        app.request_render()
        i += 1
        try:
            await asyncio.wait_for(asyncio.shield(wait_task), timeout=0.3)
            break
        except asyncio.TimeoutError:
            continue

    await drain_task
    output = b"".join(chunks).decode(errors="replace").strip()

    if proc.returncode != 0:
        tail = "\n".join(output.splitlines()[-5:])
        app._flash_status(f"Update failed:\n{tail}", seconds=10)
        app._static_hint_text()
        return

    from micro_cc.self_heal_ import evict_stock_office_libs
    err = await asyncio.to_thread(evict_stock_office_libs)
    if err:
        app._flash_status(f"Update installed, but removing stock python-pptx/openpyxl failed:\n{err}", seconds=10)
        app._static_hint_text()
        return

    app.static_hintbar.update(f"[{_pink()}]✓ updated {current_version} → {latest} — restarting…[/{_pink()}]")
    app.request_render()
    await asyncio.sleep(0.8)

    # Re-exec via `-m` to force fresh sys.path lookup; sys.argv[0] may be pre-update snapshot.
    from micro_cc.utils.self_reload_ import exec_relaunch

    exec_relaunch()


async def cmd_reload(app):
    """Re-exec to pick up source edits; /update installs from PyPI instead."""
    # Separate from auto-reload to force restart now rather than waiting for next idle.
    app.prompt.clear()
    if app._input_mode != "idle":
        app._flash_status("⏳ busy — esc first", seconds=3)
        return
    # Use same blocker check as auto-reload to avoid restarting under live subagents or GUI.
    blocker = app._self_reload_blocker()
    if blocker:
        app._flash_status(f"↻ can't reload — {blocker} active", seconds=4)
        return
    await app._restart_self()


async def cmd_author(app):
    await app._mount_row({
        "type": "text",
        "content": "author: George Juraj Salapa — https://gsequist.github.io/",
    })
    app.prompt.clear()


async def _run_hidden_setup(app, prompt_body: str, extra: str, busy_msg: str):
    if app._input_mode == "query_active":
        app._flash_status(busy_msg)
        app.prompt.clear()
        return
    setup_prompt = (
        "<system-reminder>\n"
        + prompt_body
        + (f"They also said: {extra}\n" if extra else "")
        + "</system-reminder>"
    )
    app.prompt.clear()
    if not has_configured_endpoint():
        err = {"type": "error", "content": "No API endpoint configured — run /login"}
        await app._mount_row(err)
        return
    app._input_mode = "query_active"
    import asyncio
    asyncio.create_task(app.do_query(setup_prompt))


async def cmd_setup(app, extra: str = ""):
    await _run_hidden_setup(app, hidden_prompts.SETUP, extra, "⏳ busy — try /setup again once this finishes")


async def cmd_new_skill(app, extra: str = ""):
    await _run_hidden_setup(app, hidden_prompts.SKILL, extra, "⏳ busy — try /new-skill again once this finishes")


async def cmd_new_mcp(app, extra: str = ""):
    await _run_hidden_setup(app, hidden_prompts.MCP, extra, "⏳ busy — try /new-mcp again once this finishes")


async def cmd_skills(app, extra: str = ""):
    await _run_hidden_setup(app, hidden_prompts.SKILLS_INFO, extra, "⏳ busy — try /skills again once this finishes")


async def cmd_mcp(app, extra: str = ""):
    await _run_hidden_setup(app, hidden_prompts.MCP_INFO, extra, "⏳ busy — try /mcp again once this finishes")


async def cmd_headless_mode(app, extra: str = ""):
    await _run_hidden_setup(app, hidden_prompts.HEADLESS, extra, "⏳ busy — try /headless again once this finishes")


async def cmd_graph(app, extra: str = ""):
    await _run_hidden_setup(app, hidden_prompts.GRAPH, extra, "⏳ busy — try /graph again once this finishes")


async def cmd_message_session(app, extra: str = ""):
    await _run_hidden_setup(app, hidden_prompts.MESSAGE_SESSION, extra, "⏳ busy — try /message-session again once this finishes")


async def cmd_doctor(app, extra: str = ""):
    await _run_hidden_setup(app, hidden_prompts.DOCTOR, extra, "⏳ busy — try /doctor again once this finishes")


async def cmd_optimize(app, extra: str = ""):
    await _run_hidden_setup(app, hidden_prompts.OPTIMIZE, extra, "⏳ busy — try /optimize again once this finishes")


def _kill_port(port: int):
    try:
        out = subprocess.run(
            ["lsof", "-ti", f"tcp:{port}"], capture_output=True, text=True, timeout=2,
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return
    for pid in out.split():
        try:
            os.kill(int(pid), signal.SIGTERM)
        except (ValueError, ProcessLookupError, PermissionError):
            pass


_SUBAGENT_TERMINAL_STATUSES = {"DONE", "FAILED", "PAUSED"}


def _pending_subagents(project_dir: str) -> list:
    from micro_cc.utils import subagent_tracker_
    data = subagent_tracker_.read_tracked(project_dir)
    return [
        target for target, entry in data["subagents"].items()
        if entry["status"] not in _SUBAGENT_TERMINAL_STATUSES
    ]


async def cmd_gui(app):
    app.prompt.clear()

    if getattr(app, "_gui_shutdown", None) is not None:
        app._flash_status(f"▸ already running at {app._gui_url}")
        return

    if app._input_mode == "query_active":
        app._flash_status("⏳ busy — try /gui again once this finishes")
        return

    pending = _pending_subagents(app._project_dir)
    if pending:
        app._flash_status(
            f"✗ can't start GUI — {len(pending)} subagent(s) still running "
            "(monitoring/wakeup only work in the terminal, see /graph)"
        )
        return

    if not has_configured_endpoint():
        await app._mount_row({"type": "error", "content": "No API endpoint configured — run /login"})
        return

    from micro_cc.webui.launch import serve, PORT

    def _serve():
        return serve(
            app._project_dir,
            model=app._current_model,
            dangerous=app._dangerous_tools,
            trim_budget=app._trim_budget,
        )

    try:
        url, shutdown = _serve()
    except OSError:
        _kill_port(PORT)
        time.sleep(0.3)
        try:
            url, shutdown = _serve()
        except OSError as e:
            await app._mount_row({"type": "error", "content": str(e)})
            return
    app._gui_url = url
    app._gui_shutdown = shutdown
    app._input_mode = "gui"

    await app._mount_row({
        "type": "text",
        "content": (
            f"▸ **GUI running** — [{url}]({url})\n\n"
            "Copied to your clipboard — paste it into a browser if the link "
            "above isn't clickable (common when this terminal is attached "
            "through Docker/SSH, where OSC 8 hyperlinks and text selection "
            "don't reach the outer terminal). This terminal is read-only "
            "until you close it; press **esc** to stop the server and come "
            "back."
        ),
    })
    app.prompt.placeholder = "see browser — esc to stop the GUI"
    app._refresh_status()
    app.copy_to_clipboard(url)


def stop_gui(app):
    """Tear down GUI server; safe to call twice."""
    shutdown = getattr(app, "_gui_shutdown", None)
    if shutdown is None:
        return False
    shutdown()
    app._gui_shutdown = None
    app._gui_url = None
    app._input_mode = "idle"
    app.prompt.placeholder = ""
    app.set_focus(app.prompt)
    return True


async def cmd_keys(app):
    app._input_mode = "keys"
    app._keys_stage = "name"
    app._keys_pending_name = None
    app._keys_values = {}
    app.prompt.clear()
    app.prompt.placeholder = "Env var name to store (e.g. AZURE_CLIENT_SECRET) — blank to finish"
    app.set_focus(app.prompt)


PREFIX_COMMANDS = {
    "/setup": cmd_setup,
    "/new-skill": cmd_new_skill,
    "/new-mcp": cmd_new_mcp,
    "/skills": cmd_skills,
    "/mcp": cmd_mcp,
    "/headless": cmd_headless_mode,
    "/graph": cmd_graph,
    "/message-session": cmd_message_session,
    "/doctor": cmd_doctor,
    "/optimize": cmd_optimize,
}


SLASH_COMMANDS = {
    "/clear": cmd_clear,
    "/copy": cmd_copy,
    "/model": cmd_model,
    "/theme": cmd_theme,
    "/dangerous": cmd_dangerous,
    "/subagents": cmd_subagents,
    "/rewind": cmd_rewind,
    "/login": cmd_login,
    "/keys": cmd_keys,
    "/exit": cmd_exit,
    "/quit": cmd_exit,
    "/update": cmd_update,
    "/reload": cmd_reload,
    "/author": cmd_author,
    "/gui": cmd_gui,
}

_registry_prompt_names = {c["name"] for c in command_registry.COMMANDS if c["kind"] == "prompt"}
_registry_other_names = {c["name"] for c in command_registry.COMMANDS if c["kind"] != "prompt"}
assert set(PREFIX_COMMANDS) == _registry_prompt_names, (
    f"PREFIX_COMMANDS out of sync with command_registry.py: "
    f"{set(PREFIX_COMMANDS) ^ _registry_prompt_names}"
)
assert _registry_other_names <= set(SLASH_COMMANDS), (
    f"SLASH_COMMANDS missing handlers for: {_registry_other_names - set(SLASH_COMMANDS)}"
)
