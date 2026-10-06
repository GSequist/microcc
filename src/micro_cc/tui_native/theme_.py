"""Styling as plain functions for tui_native components; a Theme replaces CSS selectors."""

from dataclasses import dataclass
from typing import Callable

RESET = "\x1b[0m"


def _wrap(*codes: str) -> Callable[[str], str]:
    prefix = "".join(codes)
    return lambda text: f"{prefix}{text}{RESET}"


@dataclass
class Theme:
    selected_text: Callable[[str], str]
    description: Callable[[str], str]
    scroll_info: Callable[[str], str]
    no_match: Callable[[str], str]
    checked: Callable[[str], str]


def _sgr(hexcode: str) -> str:
    """#rrggbb -> SGR foreground sequence (pickers paint raw ANSI, not Rich)."""
    r, g, b = int(hexcode[1:3], 16), int(hexcode[3:5], 16), int(hexcode[5:7], 16)
    return f"\x1b[38;2;{r};{g};{b}m"


def default_theme() -> Theme:
    """The picker Theme from the active color set; only `checked` is a real color."""
    from micro_cc.utils import theme_store_
    return Theme(
        selected_text=_wrap("\x1b[7m"),        # inverse video — picker cursor row
        description=_wrap("\x1b[2m"),          # dim
        scroll_info=_wrap("\x1b[2m"),          # dim
        no_match=_wrap("\x1b[2;3m"),           # dim italic
        checked=_wrap(_sgr(theme_store_.get("ok"))),  # a checked multi-select item
    )


DEFAULT_THEME = default_theme()
