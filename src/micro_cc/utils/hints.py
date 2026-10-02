"""One list of tips, shown by both front ends.

The TUI rotates these through its hint bar; the GUI drops them into the input
placeholder once a turn settles. Same copy either way, so a tip added here
shows up in both without anyone remembering to mirror it.

`where` scopes a tip to the surface it makes sense on — telling someone
already looking at the browser that /gui opens the browser is noise.
Presentation (the ⌖ prefix both surfaces use) belongs to the surface, not to
the text.

Keep these SHORT — they land in one line of chrome next to the status bar.
"""

TUI = "tui"
GUI = "gui"
BOTH = (TUI, GUI)

HINTS = [
    {"text": "Ask AI for a powerpoint deck", "where": BOTH},
    {"text": "Ask AI for art via its canvas skill", "where": BOTH},
    {"text": "Drop files into the input, AI finds them", "where": BOTH},
    {"text": "Ask AI for a spreadsheet", "where": BOTH},
    {"text": "Drag in images — AI can see them", "where": BOTH},
    {"text": "Build a new skill with /new-skill", "where": BOTH},
    {"text": "/new-skill builds skills, ex: your .pptx template", "where": BOTH},
    {"text": "See your skills and MCPs with /skills or /mcp", "where": BOTH},
    {"text": "Gated tools ask first — change with /dangerous", "where": BOTH},
    {"text": "Switch models with /model", "where": BOTH},
    {"text": "Wrong turn? /rewind goes back", "where": BOTH},
    {"text": "/gui opens this session in the browser", "where": (TUI,)},
    {"text": "/graph spawns a team of Claudes", "where": (TUI,)},
    {"text": "@ points the model at a file", "where": BOTH},
    {"text": "/ for the command palette", "where": (GUI,)},
    {"text": "shift+enter newline, enter sends", "where": (GUI,)},
    {"text": "iterm2 or ghostty for image support", "where": BOTH},
    {"text": "/theme for colors", "where": BOTH},
]


def for_surface(surface: str) -> list:
    """Just the tip strings that belong on `surface`."""
    return [h["text"] for h in HINTS if surface in h["where"]]
