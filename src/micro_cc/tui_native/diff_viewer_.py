import difflib

from rich.syntax import Syntax
from rich.text import Text

# Matches CleanMarkdown's code-block theme; colors come from the active theme set.
from micro_cc.utils import theme_store_


def _format_range(start: int, stop: int) -> str:
    """Unified diff range format: start,length (1-length ranges as line number)."""
    length = stop - start
    beginning = start + 1 if length else start
    return f"{beginning},{length}" if length != 1 else f"{beginning}"


def _highlighted_lines(code: str, lexer_name: str) -> list[Text]:
    if not code:
        return []
    syntax = Syntax(code, lexer_name, theme=theme_store_.get("syntax"), word_wrap=False, background_color="default")
    return syntax.highlight(code).split("\n")


def build_diff_lines(old: str, new: str, file_path: str = "", context: int = 3) -> list[Text]:
    """Pygments-highlighted unified diff; old/new are highlighted once so multi-line tokens survive."""
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
