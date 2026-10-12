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
    title: Callable[[str], str]


def _sgr(hexcode: str) -> str:
    """#rrggbb -> SGR foreground sequence (pickers paint raw ANSI, not Rich)."""
    r, g, b = int(hexcode[1:3], 16), int(hexcode[3:5], 16), int(hexcode[5:7], 16)
    return f"\x1b[38;2;{r};{g};{b}m"


def default_theme() -> Theme:
    """Panel styles from the active color set; read at call time so /theme switches apply."""
    from micro_cc.utils import theme_store_
    return Theme(
        selected_text=_wrap("\x1b[1m", _sgr(theme_store_.get("accent"))),  # bold accent: cursor row
        description=_wrap("\x1b[2m"),          # dim
        scroll_info=_wrap("\x1b[2m"),          # dim
        no_match=_wrap("\x1b[2;3m"),           # dim italic
        checked=_wrap(_sgr(theme_store_.get("ok"))),
        title=_wrap("\x1b[1m"),                # bold
    )


def _mix(a: str, b: str, t: float) -> str:
    """Blend two #rrggbb colors; t=0 gives a, t=1 gives b."""
    pa = [int(a[i:i + 2], 16) for i in (1, 3, 5)]
    pb = [int(b[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x + (y - x) * t):02x}" for x, y in zip(pa, pb))


def shimmer(text: str, tick: int, base: str, glow: str, band: int = 4) -> str:
    """Rich markup with a `glow` band sweeping across `text` over `base`; one step per tick."""
    from rich.markup import escape
    pos = tick % (len(text) + 2 * band) - band  # band centre; enters and leaves off-screen
    return "".join(
        f"[{_mix(base, glow, max(0, 1 - abs(i - pos) / band))}]{escape(ch)}[/]"
        for i, ch in enumerate(text)
    )
