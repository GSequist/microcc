"""Hard-coded visual tokens (ASCII, identical on every terminal); mods override any subset via the glyphs event."""

from micro_cc import mods_

DEFAULTS = {
    "user_prompt": ">",
    "queued": "[queued]",
    "thinking": "Thinking",
    "tool_pending": "*",
    "tool": "*",
    "error": "!",
    "approval": "?",
    "approval_keys": "enter to approve · esc to reject",
    "hint_expand": "click to expand",
    "hint_collapse": "click to collapse",
    "hint_view_full": "click to view full",
    "rule": "─",
    "spinner": "|/-\\",
    "bgproc": "&",
    "watch": "@",
    "subagent": "#",
    "stalled": "!",
    "project": "dir",
    "hint": "tip:",
    "model": "model",
}

_resolved: dict | None = None


def reload() -> None:
    global _resolved
    _resolved = mods_.glyphs(DEFAULTS)


def glyph(name: str) -> str:
    if _resolved is None:
        reload()
    return _resolved.get(name, DEFAULTS[name])
