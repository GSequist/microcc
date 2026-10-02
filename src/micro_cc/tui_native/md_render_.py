"""
Customized Rich Markdown rendering.

Subclasses rich.markdown.Markdown to fix:
- CodeBlock: no full-width background, no padding (was thick panels)
- Heading h1: left-aligned, not centered
"""

from rich.markdown import Markdown, CodeBlock, Heading
from rich.syntax import Syntax
from rich.style import Style
from rich.console import Console, ConsoleOptions, RenderResult
from rich.text import Text
from rich.theme import Theme

from micro_cc.utils import theme_store_


def rich_theme() -> Theme:
    """Rich's built-in markdown styles, rebuilt from the active color set.

    Rich's own defaults were picked against a dark terminal — `markdown.code`
    is literally cyan-on-black, and the table/bullet accents are pale cyans
    that vanish on white. So these are overridden from theme_store_ rather
    than left at Rich's defaults; every value is a hex the file supplies, so
    editing theme.json restyles the markdown too.

    Built fresh per call (a Theme is cheap, and rendering is cached upstream
    per width) so a live /theme switch can't keep painting the old set.
    """
    c = theme_store_.get
    return Theme({
        "markdown.code": f"bold {c('inline_code')} on {c('code_bg')}",
        "markdown.code_block": c("code_fg"),
        "markdown.block_quote": c("quote"),
        "markdown.h2": f"bold {c('heading')} underline",
        "markdown.h3": f"bold {c('heading')}",
        "markdown.h4": f"italic {c('heading')}",
        "markdown.link": c("link"),
        "markdown.link_url": f"{c('link_url')} underline",
        "markdown.list": c("list"),
        "markdown.item.number": c("list"),
        "markdown.table.border": c("table_border"),
        "markdown.table.header": f"bold {c('table_header')}",
    })


class CleanCodeBlock(CodeBlock):
    """Code block without full-width background panel."""

    def __rich_console__(
        self, console: Console, options: ConsoleOptions
    ) -> RenderResult:
        code = str(self.text).rstrip()
        yield Syntax(
            code,
            self.lexer_name,
            theme=self.theme,
            word_wrap=True,
            padding=0,
            background_color="default",
        )


class CleanHeading(Heading):
    """Heading that's always left-aligned (h1 not centered)."""

    def __rich_console__(
        self, console: Console, options: ConsoleOptions
    ) -> RenderResult:
        text = self.text.copy()
        text.justify = "left"
        yield text


class CleanMarkdown(Markdown):
    """Markdown with clean code blocks, left-aligned headings, and body
    text pulled back from full-bright default. Markdown.style defaults to
    "none" (Style.null()), which leaves plain paragraph text with no color
    override at all — it just inherits the widget's full-intensity `$text`,
    which is what reads noticeably heavier than cc's assistant replies side
    by side in the same terminal/font (this was previously misdiagnosed as
    a terminal-font difference — it isn't, it's this). Only affects spans
    with no explicit style of their own: headings/emphasis/code keep their
    own Rich styles layered on top, same as before."""

    elements = {**Markdown.elements}
    elements["fence"] = CleanCodeBlock
    elements["code_block"] = CleanCodeBlock
    elements["heading_open"] = CleanHeading

    def __init__(self, markup: str, **kwargs):
        # code_theme is a Pygments theme NAME from the active color set — a
        # palette can't express a syntax theme, so theme.json carries it as
        # the "syntax" token (e.g. "friendly" for light, "monokai" for dark).
        # Applies to fenced blocks and inline `code` alike (Rich falls back
        # to code_theme for inline unless inline_code_theme says otherwise).
        kwargs.setdefault("code_theme", theme_store_.get("syntax"))
        super().__init__(markup, **kwargs)
        self.style = theme_store_.get("fg")


class DimMarkdown(CleanMarkdown):
    """Same as CleanMarkdown but everything renders dim (for thinking)."""

    def __init__(self, markup: str, **kwargs):
        super().__init__(markup, **kwargs)
        self.style = "dim"


def render_md(content: str, dim: bool = False):
    """Return a Rich renderable for markdown content."""
    if dim:
        return DimMarkdown(content)
    return CleanMarkdown(content)
