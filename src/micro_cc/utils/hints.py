"""Tips for TUI/GUI; keep short (one-line)."""

TUI = "tui"
GUI = "gui"
BOTH = (TUI, GUI)

HINTS = [
    # what to ask for
    {"text": "Ask for a PowerPoint deck, a Word doc or a spreadsheet", "where": BOTH},
    {"text": "Ask for a poster or art, the design skill makes .png and .pdf", "where": BOTH},
    {"text": "Ask it to research a public GitHub repo, no auth needed", "where": BOTH},
    {"text": "Drop in images, the model can see them", "where": BOTH},
    {"text": "@ points the model at a file", "where": BOTH},
    {"text": "Ask it to remember something, memory carries across projects", "where": BOTH},
    # background work
    {"text": "Subagents report back on their own, the model wakes when they finish", "where": BOTH},
    {"text": "Ask it to watch a log or command, matching lines wake the model", "where": BOTH},
    {"text": "Ask it to schedule a prompt, once or on a cron", "where": BOTH},
    {"text": "Subagents and background processes show in the line above the input", "where": (GUI,)},
    {"text": "ctrl+b lists background processes and watches", "where": (TUI,)},
    {"text": "shift+tab views a subagent's conversation", "where": (TUI,)},
    {"text": "/graph orchestrates a team of subagents toward a goal", "where": (TUI,)},
    {"text": "/optimize runs a measure-change-keep-or-revert loop against a metric", "where": (TUI,)},
    {"text": "/headless runs one prompt unattended, cron-friendly", "where": BOTH},
    {"text": "/message-session talks to another running micro-cc session", "where": BOTH},
    # skills
    {"text": "/new-skill turns a template or workflow into a skill, ex: your .pptx template", "where": BOTH},
    {"text": "/skills and /mcp list what you have and how to add more", "where": BOTH},
    # control
    {"text": "Gated tools ask first, change them with /dangerous", "where": BOTH},
    {"text": "/model switches models", "where": BOTH},
    {"text": "Wrong turn? /rewind goes back", "where": BOTH},
    {"text": "/clear erases this project's conversation", "where": BOTH},
    {"text": "/copy copies the transcript", "where": BOTH},
    {"text": "/setup configures settings and the statusline", "where": BOTH},
    {"text": "/doctor diagnoses a broken install", "where": BOTH},
    {"text": "esc interrupts the model", "where": (TUI,)},
    {"text": "/keys stores project secrets the model never sees", "where": (TUI,)},
    {"text": "/theme for colors, or edit ~/.micro-cc/theme.json", "where": (TUI,)},
    {"text": "iterm2 or ghostty for image support", "where": (TUI,)},
    {"text": "/reload restarts into edits to micro-cc's own source", "where": (TUI,)},
    {"text": "/update installs the latest micro-cc", "where": (TUI,)},
    # surface specific
    {"text": "/gui opens this session in the browser", "where": (TUI,)},
    {"text": "esc in the terminal ends the GUI and takes the session back", "where": (GUI,)},
    {"text": "/ for the command palette", "where": (GUI,)},
    {"text": "shift+enter newline, enter sends", "where": (GUI,)},
    {"text": "Click the model or the gated count in the header to change them", "where": (GUI,)},
]


def for_surface(surface: str) -> list:
    """Return tips for given surface (tui/gui)."""
    return [h["text"] for h in HINTS if surface in h["where"]]
