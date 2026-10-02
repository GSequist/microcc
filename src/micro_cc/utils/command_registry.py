"""Single source of truth for available slash commands.

Both the TUI's autocomplete dropdown (start_live_.py) and the GUI's command
palette (webui/server.py) read their command list from COMMANDS below
instead of keeping their own — this is what broke when /headless and /batch
landed only in the TUI and were missed in the GUI. Add a command here once
and both surfaces pick it up; screens/screen_cmds_.py asserts its handler
dicts stay in sync with this list, so a command with no handler (or a
handler with no registry entry) fails fast at import time instead of
silently missing from one surface.

This module is metadata only — it does not dispatch anything itself:
  - "prompt" kind commands push a hidden <system-reminder> turn; the body
    lives in hidden_prompts.PROMPT_BODIES under the same name.
  - "action"/"ui"/"tui" kind commands are handled directly by each surface
    (screens/screen_cmds_.py for the TUI, inline in webui/server.py for the
    GUI) — kind just tells the GUI whether to run it, handle it client-side,
    or refuse (tui commands handle secrets and the GUI won't touch those).

`gui=False` marks commands that only make sense inside the TUI itself
(quitting the process, self-update, handing off to the browser) and are
omitted from the GUI's command palette.
"""

COMMANDS = [
    {"name": "/model", "hint": "switch model", "kind": "ui"},
    {"name": "/theme", "hint": "switch color theme (or edit ~/.micro-cc/theme.json)", "kind": "ui"},
    {"name": "/dangerous", "hint": "which tools need approval", "kind": "ui"},
    {"name": "/subagents", "hint": "max headless subagents running at once (default 4)", "kind": "ui", "gui": False},
    {"name": "/rewind", "hint": "undo back to an earlier turn", "kind": "ui"},
    {"name": "/clear", "hint": "erase this project's conversation", "kind": "action"},
    {"name": "/copy", "hint": "copy transcript to clipboard", "kind": "ui"},
    {"name": "/setup", "hint": "configure settings + statusline", "kind": "prompt", "takes_args": True},
    {"name": "/new-skill", "hint": "create a new skill", "kind": "prompt", "takes_args": True},
    {"name": "/new-mcp", "hint": "register a new MCP server", "kind": "prompt", "takes_args": True},
    {"name": "/skills", "hint": "list skills you have + how to add more", "kind": "prompt", "takes_args": True},
    {"name": "/mcp", "hint": "list MCP servers you have + how to add more", "kind": "prompt", "takes_args": True},
    {
        "name": "/headless",
        "hint": "run a single prompt headlessly (microcc-headless, the -p mode) — cron-friendly, no manifest",
        "kind": "prompt",
        "takes_args": True,
    },
    {
        "name": "/graph",
        "hint": "orchestrate multiple headless subagents toward a goal — diagram first, then spawn/monitor/adjust",
        "kind": "prompt",
        "takes_args": True,
        # TUI-only: subagent checkpoint wakeup only reaches the Textual app
        # (a Textual set_interval, see start_live_.py's _poll_subagents_tick)
        # — a browser-driven session gets no notification at all if a
        # subagent finishes/pauses, so starting orchestration from here
        # would silently promise something it can't deliver.
        "gui": False,
    },
    {
        "name": "/message-session",
        "hint": "message another micro-cc session (live or headless) by project_dir",
        "kind": "prompt",
        "takes_args": True,
    },
    {
        "name": "/optimize",
        "hint": "start/resume an autonomous measure-change-keep-or-revert loop against a metric",
        "kind": "prompt",
        "takes_args": True,
        # TUI-only: the live .auto/log.jsonl status line above the prompt
        # input only exists in start_live_tui_.py's poller — a browser
        # session has no ambient display for round-by-round progress, same
        # reasoning as /graph being TUI-only above.
        "gui": False,
    },
    {
        "name": "/doctor",
        "hint": "diagnose a broken/fragile install (pipx, uv, Homebrew-managed python…) and hand you the fix commands",
        "kind": "prompt",
        "takes_args": True,
    },
    {"name": "/author", "hint": "credit", "kind": "action"},
    {"name": "/login", "hint": "endpoint + API key", "kind": "tui"},
    {
        "name": "/keys",
        "hint": (
            "store project-scoped secrets — typed values never reach the model, "
            "written straight to {project_dir}/.env, auto-sourced into every "
            "bash_ call; no vaulting beyond that"
        ),
        "kind": "tui",
    },
    {"name": "/exit", "hint": "quit", "kind": "action", "gui": False},
    {"name": "/update", "hint": "install latest micro-cc", "kind": "action", "gui": False},
    {
        "name": "/reload",
        "hint": "restart to pick up edits to micro-cc's own source (no PyPI install)",
        "kind": "action",
        "gui": False,
    },
    {"name": "/gui", "hint": "open this session in the browser", "kind": "action", "gui": False},
]
