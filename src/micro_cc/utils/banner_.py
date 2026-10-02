"""BANNER — the startup splash. Pure Rich-markup string, zero UI-framework
dependency, so it lives in utils/ rather than tui_native/.

Colors come from the active theme set (utils/theme_store_), so the splash
repaints with everything else on a /theme switch:

  banner_colors  the wordmark hue, picked at random per launch from a small
                 list — the per-launch variety is kept, but the hues now
                 come from the theme instead of being hardcoded pastels
                 (which were tuned for a dark ground and washed out on white)
  banner_dim     the shadow/dim strokes and the quote + key hints

banner() is a FUNCTION, not a module constant: a live theme switch has to be
able to rebuild the splash, so binding it once at import would freeze the
old palette for the process's whole life. start_live_tui_ calls it when
constructing the widget and again from _apply_theme.
"""

import random

from micro_cc.utils import theme_store_


def banner_color() -> str:
    """One hue from the active theme's banner list, random per call."""
    return random.choice(theme_store_.get("banner_colors"))


def _banner_template(c: str, dim: str) -> str:
    return f"""\
                   [{c}]88[/]
                   [{dim}]""[/]

[{c}]88[/][{dim}],[/][{c}]dPYba[/][{dim}],,[/][{c}]adPYba[/][{dim}],[/]  [{c}]88[/]  [{dim}],[/][{c}]adPPYba[/][{dim}],[/] [{c}]8b[/][{dim}],[/][{c}]dPPYba[/][{dim}],[/]  [{dim}],[/][{c}]adPPYba[/][{dim}],[/]     [{dim}],[/][{c}]adPPYba[/][{dim}],[/]  [{dim}],[/][{c}]adPPYba[/][{dim}],[/]
[{c}]88P[/][{dim}]'[/]   [{dim}]"[/][{c}]88[/][{dim}]"[/]    [{dim}]"[/][{c}]8a[/] [{c}]88[/] [{c}]a8[/][{dim}]"[/]     [{dim}]""[/] [{c}]88P[/][{dim}]'[/]   [{dim}]"[/][{c}]Y8[/] [{c}]a8[/][{dim}]"[/]     [{dim}]"[/][{c}]8a[/]   [{c}]a8[/][{dim}]"[/]     [{dim}]""[/] [{c}]a8[/][{dim}]"[/]     [{dim}]""[/]
[{c}]88[/]      [{c}]88[/]      [{c}]88[/] [{c}]88[/] [{c}]8b[/]         [{c}]88[/]         [{c}]8b[/]       [{c}]d8[/] [bold {c}]\u00b7[/] [{c}]8b[/]         [{c}]8b[/]
[{c}]88[/]      [{c}]88[/]      [{c}]88[/] [{c}]88[/] [{dim}]"[/][{c}]8a[/][{dim}],[/]   [{dim}],[/][{c}]aa[/] [{c}]88[/]         [{dim}]"[/][{c}]8a[/][{dim}],[/]   [{dim}],[/][{c}]a8[/][{dim}]"[/]   [{dim}]"[/][{c}]8a[/][{dim}],[/]   [{dim}],[/][{c}]aa[/] [{dim}]"[/][{c}]8a[/][{dim}],[/]   [{dim}],[/][{c}]aa[/]
[{c}]88[/]      [{c}]88[/]      [{c}]88[/] [{c}]88[/]  [{dim}]`"[/][{c}]Ybbd8[/][{dim}]"'[/] [{c}]88[/]          [{dim}]`"[/][{c}]YbbdP[/][{dim}]"'[/]     [{dim}]`"[/][{c}]Ybbd8[/][{dim}]"'[/]  [{dim}]`"[/][{c}]Ybbd8[/][{dim}]"'[/]
[{dim} italic]Knowledge work is, by extension, a coding problem.[/]

[{c}]enter[/] [{dim}]submit[/] [{dim}]\u00b7[/] [{c}]esc[/] [{dim}]interrupt[/] [{dim}]\u00b7[/] [{c}]/[/] [{dim}]commands[/]
"""


def banner() -> tuple[str, str]:
    """(splash_markup, chosen_hue). The hue is returned so anything that
    wants to match the wordmark (the hint bar historically did) can, without
    picking a second random color of its own."""
    c = banner_color()
    return _banner_template(c, theme_store_.get("banner_dim")), c


# Picked once at import so the very first frame (before any /theme switch)
# has a splash; start_live_tui_ rebuilds it via banner() on a theme change.
BANNER, BANNER_COLOR = banner()