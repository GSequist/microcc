import difflib

from rich.syntax import Syntax
from rich.text import Text

# Matches CleanMarkdown's own code-block theme (md_render_.py) — a diff and
# a plain code block should read as the same visual family.
# Colors come from the active theme set (utils/theme_store_) — the syntax
# theme is a Pygments name, the line tints are hex backgrounds.
from micro_cc.utils import theme_store_


def _format_range(start: int, stop: int) -> str:
    """Same range format difflib.unified_diff's hunk headers use
    (start,length — 1-length ranges collapse to just the line number)."""
    length = stop - start
    beginning = start + 1 if length else start
    return f"{beginning},{length}" if length != 1 else f"{beginning}"


def _highlighted_lines(code: str, lexer_name: str) -> list[Text]:
    if not code:
        return []
    syntax = Syntax(code, lexer_name, theme=theme_store_.get("syntax"), word_wrap=False, background_color="default")
    return syntax.highlight(code).split("\n")


def build_diff_lines(old: str, new: str, file_path: str = "", context: int = 3) -> list[Text]:
    """Unified diff, syntax-highlighted per the file's own language (guessed
    from file_path via Pygments, same as any other code block) rather than
    generically diff-colored. This provides real code highlighting under a +/-
    gutter and red/green line tinting, rather than just red-text/green-text.

    old_hi/new_hi are each highlighted ONCE over the full text (not per
    line) so multi-line constructs a per-line Syntax call would lose
    context on — a triple-quoted string, a multi-line comment — still
    tokenize correctly; splitting the result on "\\n" afterwards is free.
    """
    lexer_name = Syntax.guess_lexer(file_path, code=new or old) if file_path else "text"
    old_lines = old.splitlines()
    new_lines = new.splitlines()
    old_hi = _highlighted_lines(old, lexer_name)
    new_hi = _highlighted_lines(new, lexer_name)

    matcher = difflib.SequenceMatcher(None, old_lines, new_lines, autojunk=False)
    groups = list(matcher.get_grouped_opcodes(context))

    out: list[Text] = []
    for group in groups:
        first, last = group[0], group[-1]
        header = (
            f"@@ -{_format_range(first[1], last[2])} "
            f"+{_format_range(first[3], last[4])} @@"
        )
        out.append(Text(header, style="dim italic"))
        for tag, i1, i2, j1, j2 in group:
            if tag == "equal":
                for i in range(i1, i2):
                    out.append(Text("  ") + old_hi[i])
                continue
            if tag in ("delete", "replace"):
                for i in range(i1, i2):
                    line = Text("- ", style=f"bold {theme_store_.get('diff_del_fg')}") + old_hi[i]
                    line.stylize(f"on {theme_store_.get('diff_del_bg')}")
                    out.append(line)
            if tag in ("insert", "replace"):
                for j in range(j1, j2):
                    line = Text("+ ", style=f"bold {theme_store_.get('diff_add_fg')}") + new_hi[j]
                    line.stylize(f"on {theme_store_.get('diff_add_bg')}")
                    out.append(line)
    return out


if __name__ == "__main__":
    from rich.console import Console

    text1 = """def foo(x):
    y = x + 1
    return y
"""
    text2 = """def foo(x):
    y = x + 2
    return y

def bar():
    pass
"""
    Console().print(*build_diff_lines(text1, text2, file_path="example.py"))
