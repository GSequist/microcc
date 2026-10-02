"""Single-line truncation-with-ellipsis component. Provides unpadded
truncation with a default "..." ellipsis. Reuses text_utils_.py's
slice_by_column for ANSI-aware slicing rather than re-deriving it — same
width-1-per-char caveat documented in that module's docstring (no grapheme
segmentation).
"""

from micro_cc.tui_native.text_utils_ import slice_by_column, visible_width

RESET = "\x1b[0m"


def truncate_to_width(text: str, max_width: int, ellipsis: str = "...") -> str:
    """Unpadded truncation: if text fits, return it untouched (ANSI and
    all); otherwise clip to max_width - len(ellipsis) visible columns and
    append the ellipsis, with a reset code before and after it so it never
    inherits — or leaks — the clipped text's styling."""
    if max_width <= 0 or not text:
        return ""
    if visible_width(text) <= max_width:
        return text

    ellipsis_w = visible_width(ellipsis)
    if ellipsis_w >= max_width:
        return slice_by_column(ellipsis, 0, max_width, True)

    target_width = max_width - ellipsis_w
    prefix = slice_by_column(text, 0, target_width, True)
    return f"{prefix}{RESET}{ellipsis}{RESET}" if ellipsis else f"{prefix}{RESET}"


class TruncatedText:
    """Component protocol (render/invalidate). Always renders exactly one
    content line (plus padding_y blank lines above/below), stopping at the
    first newline and truncating with an ellipsis if it doesn't fit."""

    def __init__(self, text: str = "", padding_x: int = 0, padding_y: int = 0):
        self.text = text
        self.padding_x = padding_x
        self.padding_y = padding_y

    def set_text(self, text: str) -> None:
        self.text = text

    def invalidate(self) -> None:
        pass  # no cached state to invalidate

    def render(self, width: int) -> list[str]:
        result: list[str] = []
        empty_line = " " * width
        result.extend([empty_line] * self.padding_y)

        available_width = max(1, width - self.padding_x * 2)
        newline_index = self.text.find("\n")
        single_line_text = self.text if newline_index == -1 else self.text[:newline_index]
        display_text = truncate_to_width(single_line_text, available_width)

        line_with_padding = " " * self.padding_x + display_text + " " * self.padding_x
        padding_needed = max(0, width - visible_width(line_with_padding))
        result.append(line_with_padding + " " * padding_needed)

        result.extend([empty_line] * self.padding_y)
        return result
