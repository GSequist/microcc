"""ANSI-code-aware string helpers; full escape-sequence fidelity, width-1-per-char."""

import re

# --- extract_ansi_code -------------------------------------------------
# Recognize CSI, OSC, APC escape sequences (termination varies by type).
_CSI_TERM_RE = re.compile(r"[mGKHJ]")


def extract_ansi_code(s: str, pos: int) -> tuple[str, int] | None:
    """Extract escape sequence at s[pos]; return (code, length) or None."""
    if pos >= len(s) or s[pos] != "\x1b":
        return None
    nxt = s[pos + 1] if pos + 1 < len(s) else ""

    if nxt == "[":
        m = _CSI_TERM_RE.search(s, pos + 2)
        if m:
            j = m.end() - 1
            return s[pos:j + 1], j + 1 - pos
        return None

    if nxt in ("]", "_"):
        # Terminated by BEL or by ESC \ (ST) — whichever occurs first.
        bel = s.find("\x07", pos + 2)
        st = s.find("\x1b\\", pos + 2)
        if bel == -1 and st == -1:
            return None
        if st == -1 or (bel != -1 and bel < st):
            return s[pos:bel + 1], bel + 1 - pos
        return s[pos:st + 2], st + 2 - pos

    return None


def strip_terminal_sequences(s: str) -> str:
    """Remove all escape sequences; used for plain-text measurement."""
    if "\x1b" not in s:
        return s
    out = []
    i = 0
    while i < len(s):
        ansi = extract_ansi_code(s, i)
        if ansi:
            i += ansi[1]
            continue
        out.append(s[i])
        i += 1
    return "".join(out)


def visible_width(s: str) -> int:
    """Length of plain text, escape sequences excluded."""
    return len(strip_terminal_sequences(s))


_OSC8_RE = re.compile(r"^\x1b\]8;[^;]*;([^\x07\x1b]*)(?:\x07|\x1b\\)$")


def get_osc8_link_at_column(line: str, column: int) -> str | None:
    """Get OSC-8 hyperlink URL active at column, tracking through line."""
    active_url: str | None = None
    current_col = 0
    i = 0
    while i < len(line):
        ansi = extract_ansi_code(line, i)
        if ansi:
            code, length = ansi
            m = _OSC8_RE.match(code)
            if m:
                active_url = m.group(1) or None
            i += length
            continue
        # one plain character = one cell (see docstring)
        if column == current_col:
            return active_url
        current_col += 1
        i += 1
    return None


def slice_with_width(line: str, start_col: int, length: int, strict: bool = False) -> tuple[str, int]:
    """Slice columns [start_col, start_col+length); preserve ANSI codes inside range."""
    if length <= 0:
        return "", 0
    end_col = start_col + length
    result = []
    result_width = 0
    current_col = 0
    i = 0
    pending_ansi = ""
    while i < len(line):
        ansi = extract_ansi_code(line, i)
        if ansi:
            code, alen = ansi
            if start_col <= current_col < end_col:
                result.append(code)
            elif current_col < start_col:
                pending_ansi += code
            i += alen
            continue
        w = 1  # one char = one cell, see module docstring
        in_range = start_col <= current_col < end_col
        fits = (not strict) or (current_col + w <= end_col)
        if in_range and fits:
            if pending_ansi:
                result.append(pending_ansi)
                pending_ansi = ""
            result.append(line[i])
            result_width += w
        current_col += w
        i += 1
        if current_col >= end_col:
            break
    return "".join(result), result_width


def slice_by_column(line: str, start_col: int, length: int, strict: bool = False) -> str:
    return slice_with_width(line, start_col, length, strict)[0]


def extract_segments(
    line: str, before_end: int, after_start: int, after_len: int, strict_after: bool = False,
) -> tuple[str, int, str, int]:
    """Extract before [0, before_end) and after [after_start, after_start+after_len) fragments."""
    before = []
    before_width = 0
    after = []
    after_width = 0
    current_col = 0
    i = 0
    pending_ansi_before = ""
    after_started = False
    after_end = after_start + after_len

    while i < len(line):
        ansi = extract_ansi_code(line, i)
        if ansi:
            code, alen = ansi
            if current_col < before_end:
                pending_ansi_before += code
            elif after_start <= current_col < after_end and after_started:
                after.append(code)
            i += alen
            continue
        w = 1
        if current_col < before_end and current_col + w <= before_end:
            if pending_ansi_before:
                before.append(pending_ansi_before)
                pending_ansi_before = ""
            before.append(line[i])
            before_width += w
        elif after_start <= current_col < after_end:
            fits = (not strict_after) or (current_col + w <= after_end)
            if fits:
                if not after_started:
                    after_started = True
                after.append(line[i])
                after_width += w
        current_col += w
        i += 1
        done = current_col >= before_end if after_len <= 0 else current_col >= after_end
        if done:
            break
    return "".join(before), before_width, "".join(after), after_width
