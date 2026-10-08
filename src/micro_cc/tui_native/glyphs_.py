"""Hard-coded visual tokens; mods override any subset via the glyphs event."""

import os

from micro_cc import mods_

DEFAULTS = {
    "user_prompt": "›",
    "queued": "⋯queued",
    "thinking": "∴ Thinking",
    "tool_pending": "◇",
    "tool": "⟐",
    "error": "△",
    "approval": "◆",
    "approval_keys": "enter to approve · esc to reject",
    "hint_expand": "click to expand",
    "hint_collapse": "click to collapse",
    "hint_view_full": "click to view full",
    "rule": "─",
    "spinner": "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏",
    "bgproc": "◇",
    "watch": "◎",
    "stalled": "⚠",
    "project": "⏣",
    "hint": "⌖",
    "model": "◈",
}

# Nerd Font icons: cell-width glyphs drawn to match the monospace font, unlike Unicode symbol fallbacks.
NERD = {
    "project": "\U000f0256",  # md-folder_outline
    "hint": "\U000f0336",     # md-lightbulb_outline
    "model": "\U000f06a9",    # md-robot
}


def nerd() -> bool:
    """True when the terminal ships Nerd Font symbols (MICRO_CC_NERD=0/1 overrides)."""
    forced = os.environ.get("MICRO_CC_NERD")
    if forced in ("0", "1"):
        return forced == "1"
    return os.environ.get("TERM_PROGRAM") in ("ghostty", "WezTerm") or os.environ.get("TERM") == "xterm-kitty"

_resolved: dict | None = None


def reload() -> None:
    global _resolved
    _resolved = mods_.glyphs({**DEFAULTS, **NERD} if nerd() else DEFAULTS)


def glyph(name: str) -> str:
    if _resolved is None:
        reload()
    return _resolved.get(name, DEFAULTS[name])
