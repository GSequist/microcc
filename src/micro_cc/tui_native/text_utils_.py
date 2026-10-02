"""ANSI-code-aware string helpers: extract/skip escape sequences so text
can be measured, sliced, and stitched back together without corrupting
color/hyperlink codes or losing track of where they were.

Full-fidelity for escape-sequence handling (that's not a Unicode-width
question, it's just "don't chop an escape code in half or lose it when
slicing"). NOT full-fidelity for grapheme width: real terminal rendering
treats wide CJK characters as 2 cells, zero-width combining marks as 0
cells, and groups multi-codepoint emoji into one cell. Here every
non-escape character counts as width 1. That's the same known, named gap
as tui_native/alt_screen_.py's clip_overwide_line — correct today because
nothing on screen is wide/combining, wrong the day it is. Fix both
together, they're the same underlying missing piece (a real Unicode
East-Asian-width table + grapheme segmenter), not two separate bugs.
"""

import re

# --- extract_ansi_code -------------------------------------------------
# Recognizes the three escape-sequence shapes that show up in rendered
# terminal output:
#   CSI   ESC [ ... terminated by one of m G K H J   (colors, cursor moves,
#         clear-line/screen — everything doRender()'s own output uses)
#   OSC   ESC ] ... terminated by BEL or ESC \        (hyperlinks OSC 8,
#         titles, clipboard OSC 52)
#   APC   ESC _ ... terminated by BEL or ESC \        (CURSOR_MARKER lives
#         here)
_CSI_TERM_RE = re.compile(r"[mGKHJ]")


def extract_ansi_code(s: str, pos: int) -> tuple[str, int] | None:
    """If s[pos] starts an escape sequence, return (code, length). Else None.

    Uses str.find/re.search (C-level scans) rather than a manual
    character-by-character Python while loop to find the terminator —
    same semantics, but a manual loop here cost ~300ms on a single
    multi-megabyte Kitty image escape sequence (do_render calls this on
    every line, every frame, via visible_width/clip_overwide_line —
    alt_screen_.py:725-735), which is what made the terminal grind to a
    halt while an image was on screen and text streamed below it."""
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
    """Remove every escape sequence extract_ansi_code recognizes, keep the
    rest verbatim. Used to measure/compare plain text (word segmentation,
    click-target text) without caring about color."""
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
    """len() of the plain text, escape sequences excluded. See module
    docstring for the wide-char caveat."""
    return len(strip_terminal_sequences(s))


_OSC8_RE = re.compile(r"^\x1b\]8;[^;]*;([^\x07\x1b]*)(?:\x07|\x1b\\)$")


def get_osc8_link_at_column(line: str, column: int) -> str | None:
    """Port of getOsc8LinkAtColumn(): walk the line tracking the most
    recently opened OSC-8 hyperlink URL, return it if `column` falls
    inside the plain-text run that followed it. Width-1-per-char, per the
    module docstring."""
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
    """Return (text, width) for the visible columns [start_col, start_col+length),
    preserving whichever ANSI codes fall inside that range. Codes that fall
    entirely before the slice are preserved if they're immediately before it,
    so the sliced fragment doesn't lose its own styling."""
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
    """Port of extractSegments(): in one pass, pull out the "before"
    fragment (columns [0, before_end)) and the "after" fragment (columns
    [after_start, after_start+after_len)), inheriting whatever SGR styling
    was active at after_start so the "after" piece doesn't lose its color
    just because its opening escape code landed inside the gap being cut
    out. compositeTuiLine (below, in alt_screen_.py) is the only caller
    that needs this; it's what lets an overlay/flash sit in the middle of
    a styled line without the tail losing its color.

    This implementation doesn't track the full SGR state seen so far to
    reconstruct "what's currently active" (bold/color/etc). If the base
    line's styling changes color mid-line and the cut lands between the
    color code and the text, the "after" fragment won't inherit it.
    Acceptable for now (nothing renders that today); a real feature gap
    if/when Rich-rendered rows get overlaid.
    """
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
