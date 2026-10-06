"""Command registry shared by TUI and GUI; gui=False marks TUI-only commands."""

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
        "gui": False,  # TUI-only: subagent checkpoint wakeup is TUI-only (set_interval).
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
        "gui": False,  # TUI-only: .auto/log.jsonl progress only in TUI poller.
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
