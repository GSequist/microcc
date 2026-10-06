"""Word-wrapping text with ANSI code preservation and minimal markup translation."""

import re
from dataclasses import dataclass, field
from typing import Callable

from micro_cc.tui_native.text_utils_ import extract_ansi_code, visible_width
from micro_cc.utils import theme_store_

RESET = "\x1b[0m"

# --- markup translation -------------------------------------------------

# Named color "red" is aliased to theme's error token; resolved at translate time.
_NAMED_COLOR_TOKENS = {"red": "error"}
_TAG_RE = re.compile(r"\[(#[0-9a-fA-F]{6}|red)\]|\[/(?:#[0-9a-fA-F]{6}|red)?\]")


def _hex_fg(hexcode: str) -> str:
    r, g, b = int(hexcode[1:3], 16), int(hexcode[3:5], 16), int(hexcode[5:7], 16)
    return f"\x1b[38;2;{r};{g};{b}m"


def translate_markup(text: str) -> str:
    """See module docstring for exactly what's supported."""
    if "[" not in text:
        return text

    def repl(m: re.Match) -> str:
        if m.group(0).startswith("[/"):
            return RESET
        color = m.group(1)
        token = _NAMED_COLOR_TOKENS.get(color)
        return _hex_fg(theme_store_.get(token)) if token else _hex_fg(color)

    return _TAG_RE.sub(repl, text)


# --- SGR state tracking (ANSI attribute tracking) -------------------
# Track attributes individually to turn off without full reset.

_TOGGLE_ATTRS = (1, 2, 3, 4, 5, 7, 8, 9)


@dataclass
class _SgrState:
    attrs: set = field(default_factory=set)
    fg: str | None = None
    bg: str | None = None

    def reset(self) -> None:
        self.attrs.clear()
        self.fg = None
        self.bg = None


def _process_sgr(state: _SgrState, code: str) -> None:
    if not code.endswith("m"):
        return
    m = re.match(r"\x1b\[([\d;]*)m", code)
    if not m:
        return
    params = m.group(1)
    if params in ("", "0"):
        state.reset()
        return
    parts = params.split(";")
    i = 0
    while i < len(parts):
        n = int(parts[i]) if parts[i] else 0
        if n in (38, 48):
            if i + 2 < len(parts) and parts[i + 1] == "5":
                color = ";".join(parts[i:i + 3])
                if n == 38:
                    state.fg = color
                else:
                    state.bg = color
                i += 3
                continue
            if i + 4 < len(parts) and parts[i + 1] == "2":
                color = ";".join(parts[i:i + 5])
                if n == 38:
                    state.fg = color
                else:
                    state.bg = color
                i += 5
                continue
        if n == 0:
            state.reset()
        elif n in _TOGGLE_ATTRS:
            state.attrs.add(n)
        elif n == 21:
            state.attrs.discard(1)
        elif n == 22:
            state.attrs.discard(1)
            state.attrs.discard(2)
        elif n in (23, 24, 25, 27, 28, 29):
            state.attrs.discard(n - 20 if n != 27 else 7)
        elif n == 39:
            state.fg = None
        elif n == 49:
            state.bg = None
        elif (30 <= n <= 37) or (90 <= n <= 97):
            state.fg = str(n)
        elif (40 <= n <= 47) or (100 <= n <= 107):
            state.bg = str(n)
        i += 1


def _active_codes(state: _SgrState) -> str:
    codes = [str(a) for a in sorted(state.attrs)]
    if state.fg:
        codes.append(state.fg)
    if state.bg:
        codes.append(state.bg)
    return f"\x1b[{';'.join(codes)}m" if codes else ""


def _line_end_reset(state: _SgrState) -> str:
    return "\x1b[24m" if 4 in state.attrs else ""


def _update_tracker_from_text(text: str, state: _SgrState) -> None:
    i = 0
    while i < len(text):
        ansi = extract_ansi_code(text, i)
        if ansi:
            _process_sgr(state, ansi[0])
            i += ansi[1]
        else:
            i += 1


# --- tokenizing + wrapping ------------------------------------------------
# Simplified width model (1 char = 1 width); no grapheme segmentation.

def _split_tokens_with_ansi(text: str) -> list[str]:
    tokens: list[str] = []
    current = ""
    pending_ansi = ""
    current_kind: str | None = None
    i = 0
    while i < len(text):
        ansi = extract_ansi_code(text, i)
        if ansi:
            pending_ansi += ansi[0]
            i += ansi[1]
            continue
        ch = text[i]
        kind = "space" if ch == " " else "word"
        if current and current_kind != kind:
            tokens.append(current)
            current = ""
        if pending_ansi:
            current += pending_ansi
            pending_ansi = ""
        current_kind = kind
        current += ch
        i += 1
    if pending_ansi:
        if current:
            current += pending_ansi
        elif tokens:
            tokens[-1] += pending_ansi
        else:
            current = pending_ansi
    if current:
        tokens.append(current)
    return tokens


def _break_long_word(word: str, width: int, state: _SgrState) -> list[str]:
    lines: list[str] = []
    current_line = _active_codes(state)
    current_width = 0
    i = 0
    while i < len(word):
        ansi = extract_ansi_code(word, i)
        if ansi:
            current_line += ansi[0]
            _process_sgr(state, ansi[0])
            i += ansi[1]
            continue
        if current_width + 1 > width:
            reset = _line_end_reset(state)
            if reset:
                current_line += reset
            lines.append(current_line)
            current_line = _active_codes(state)
            current_width = 0
        current_line += word[i]
        current_width += 1
        i += 1
    if current_line:
        lines.append(current_line)
    return lines if lines else [""]


def _wrap_single_line(line: str, width: int, strip_trailing: bool = True) -> list[str]:
    if not line:
        return [""]
    if visible_width(line) <= width:
        return [line]

    wrapped: list[str] = []
    state = _SgrState()
    tokens = _split_tokens_with_ansi(line)
    current_line = ""
    current_visible_len = 0

    for token in tokens:
        token_visible_len = visible_width(token)
        is_whitespace = token.strip() == ""

        if token_visible_len > width and not is_whitespace:
            if current_line:
                reset = _line_end_reset(state)
                if reset:
                    current_line += reset
                wrapped.append(current_line)
                current_line = ""
                current_visible_len = 0
            broken = _break_long_word(token, width, state)
            wrapped.extend(broken[:-1])
            current_line = broken[-1]
            current_visible_len = visible_width(current_line)
            continue

        total_needed = current_visible_len + token_visible_len
        if total_needed > width and current_visible_len > 0:
            line_to_wrap = current_line.rstrip() if strip_trailing else current_line
            reset = _line_end_reset(state)
            if reset:
                line_to_wrap += reset
            wrapped.append(line_to_wrap)
            if is_whitespace:
                current_line = _active_codes(state)
                current_visible_len = 0
            else:
                current_line = _active_codes(state) + token
                current_visible_len = token_visible_len
        else:
            current_line += token
            current_visible_len += token_visible_len

        _update_tracker_from_text(token, state)

    if current_line:
        wrapped.append(current_line)

    if not strip_trailing:
        return wrapped if wrapped else [""]
    return [ln.rstrip() for ln in wrapped] if wrapped else [""]


def wrap_text_with_ansi(text: str, width: int, strip_trailing: bool = True) -> list[str]:
    """Word-wrap preserving ANSI codes; strip_trailing controls space handling at breaks."""
    if not text:
        return [""]
    input_lines = re.split(r"\r\n|\r|\n", text)
    result: list[str] = []
    state = _SgrState()
    for input_line in input_lines:
        prefix = _active_codes(state) if result else ""
        result.extend(_wrap_single_line(prefix + input_line, width, strip_trailing))
        _update_tracker_from_text(input_line, state)
    return result if result else [""]


def _apply_background(line: str, width: int, bg_fn: Callable[[str], str]) -> str:
    padding_needed = max(0, width - visible_width(line))
    return bg_fn(line + " " * padding_needed)


class TextLine:
    """Multi-line word-wrapped text block with optional padding."""

    def __init__(self, text: str = "", padding_x: int = 1, padding_y: int = 1,
                 custom_bg_fn: Callable[[str], str] | None = None):
        self.text = text
        self.padding_x = padding_x
        self.padding_y = padding_y
        self.custom_bg_fn = custom_bg_fn
        self._cached_text: str | None = None
        self._cached_width: int | None = None
        self._cached_lines: list[str] | None = None

    def set_text(self, text: str) -> None:
        self.text = text
        self.invalidate()

    def set_custom_bg_fn(self, custom_bg_fn: Callable[[str], str] | None) -> None:
        self.custom_bg_fn = custom_bg_fn
        self.invalidate()

    def invalidate(self) -> None:
        self._cached_text = None
        self._cached_width = None
        self._cached_lines = None

    def render(self, width: int) -> list[str]:
        if (self._cached_lines is not None and self._cached_text == self.text
                and self._cached_width == width):
            return self._cached_lines

        if not self.text or not self.text.strip():
            result: list[str] = []
            self._cached_text, self._cached_width, self._cached_lines = self.text, width, result
            return result

        normalized_text = translate_markup(self.text).replace("\t", "   ")

        padding_x = min(self.padding_x, max(0, (width - 1) // 2))
        content_width = max(1, width - padding_x * 2)

        wrapped_lines = wrap_text_with_ansi(normalized_text, content_width)

        left_margin = " " * padding_x
        right_margin = " " * padding_x
        content_lines: list[str] = []
        for line in wrapped_lines:
            line_with_margins = left_margin + line + right_margin
            if self.custom_bg_fn:
                content_lines.append(_apply_background(line_with_margins, width, self.custom_bg_fn))
            else:
                padding_needed = max(0, width - visible_width(line_with_margins))
                content_lines.append(line_with_margins + " " * padding_needed)

        empty_line = " " * width
        emptied = self.custom_bg_fn(empty_line) if self.custom_bg_fn else empty_line
        empty_lines = [emptied] * self.padding_y

        result = empty_lines + content_lines + empty_lines
        self._cached_text, self._cached_width, self._cached_lines = self.text, width, result
        return result if result else [""]
