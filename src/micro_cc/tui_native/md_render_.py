"""Customized Rich Markdown: clean code blocks, left-aligned headings."""

from rich.markdown import Markdown, CodeBlock, Heading
from rich.syntax import Syntax
from rich.style import Style
from rich.console import Console, ConsoleOptions, RenderResult
from rich.text import Text
from rich.theme import Theme

from micro_cc.utils import theme_store_


def rich_theme() -> Theme:
    """Markdown styles from active color set; rebuilt fresh per call for live /theme switch."""
    c = theme_store_.get
    return Theme({
        "markdown.code": c("inline_code"),
        "markdown.code_block": c("code_fg"),
        "markdown.block_quote": c("quote"),
        "markdown.h1": f"{c('heading')} underline",
        "markdown.h2": c("heading"),
        "markdown.h3": c("heading"),
        "markdown.h4": f"italic {c('heading')}",
        "markdown.strong": c("heading"),
        "markdown.link": c("link"),
        "markdown.link_url": f"{c('link_url')} underline",
        "markdown.list": c("list"),
        "markdown.item.number": c("list"),
        "markdown.item.bullet": c("list"),
        "markdown.table.border": c("table_border"),
        "markdown.table.header": c("table_header"),
    })


class CleanCodeBlock(CodeBlock):
    """Code block without full-width background or padding."""

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
    """Left-aligned heading (h1 not centered)."""

    def __rich_console__(
        self, console: Console, options: ConsoleOptions
    ) -> RenderResult:
        text = self.text.copy()
        text.justify = "left"
        yield text


class CleanMarkdown(Markdown):
    """Markdown with clean code blocks, left-aligned headings, body text dimmed."""

    elements = {**Markdown.elements}
    elements["fence"] = CleanCodeBlock
    elements["code_block"] = CleanCodeBlock
    elements["heading_open"] = CleanHeading

    def __init__(self, markup: str, **kwargs):
        # code_theme is Pygments theme name from active color set (e.g. "monokai").
        kwargs.setdefault("code_theme", theme_store_.get("syntax"))
        super().__init__(markup, **kwargs)
        self.style = theme_store_.get("fg")


class DimMarkdown(CleanMarkdown):
    """CleanMarkdown with everything rendered dim (for thinking)."""

    def __init__(self, markup: str, **kwargs):
        super().__init__(markup, **kwargs)
        self.style = "dim"


def render_md(content: str, dim: bool = False):
    """Return a Rich renderable for markdown content."""
    if dim:
        return DimMarkdown(content)
    return CleanMarkdown(content)
