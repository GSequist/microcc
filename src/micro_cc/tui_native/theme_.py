"""Styling as plain functions, not a stylesheet. Replaces microcc-styles.tcss's
job for anything built in tui_native/: a component takes a Theme (or the
default one) in its constructor and calls theme.selected_text(s) instead of a
CSS selector matching it. TCSS has no equivalent here, since Textual's
cascade/selectors/$vars don't exist once App is gone."""

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
    """#rrggbb -> an SGR foreground sequence. The pickers paint raw ANSI
    (they return list[str] straight into the frame), so unlike every other
    consumer they can't hand a hex to Rich — this is the one place a hex has
    to become escape codes by hand. Parsed once per call site, not per
    character."""
    r, g, b = int(hexcode[1:3], 16), int(hexcode[3:5], 16), int(hexcode[5:7], 16)
    return f"\x1b[38;2;{r};{g};{b}m"


def default_theme() -> Theme:
    """The picker Theme, built from the active color set.

    Everything except `checked` is theme-independent by construction:
    inverse-video (the selection bar) and dim are terminal attributes that
    read correctly on either ground. Only the checked-item color is a real
    color, so it's the only token read here."""
    from micro_cc.utils import theme_store_
    return Theme(
        selected_text=_wrap("\x1b[7m"),        # inverse video — picker cursor row
        description=_wrap("\x1b[2m"),          # dim
        scroll_info=_wrap("\x1b[2m"),          # dim
        no_match=_wrap("\x1b[2;3m"),           # dim italic
        checked=_wrap(_sgr(theme_store_.get("ok"))),  # a checked multi-select item
    )


DEFAULT_THEME = default_theme()
