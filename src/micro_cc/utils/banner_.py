"""Startup splash: pure Rich-markup string; colors from active theme."""

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
    """Return (splash_markup, chosen_hue); hue for matching wordmark color."""
    c = banner_color()
    return _banner_template(c, theme_store_.get("banner_dim")), c


# Picked once at import; start_live_tui_ rebuilds on /theme switch.
BANNER, BANNER_COLOR = banner()