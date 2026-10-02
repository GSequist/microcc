"""Word navigation for alt+left/alt+right word-jump logic. Implements
findWordBackward/findWordForward segmentation and cursor movement.

Uses default UAX#29 word-break rules: one segment per CJK ideograph, standard
breaking for ASCII. Python's stdlib has no equivalent to CJK word-break
dictionaries (ICU data tables + Viterbi-style segmentation), so default_segment()
below implements plain default UAX#29 instead. This matches standard behavior
for ASCII text and degrades gracefully on CJK rather than silently.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass

# Direct port of utils.ts PUNCTUATION_REGEX. Used both to classify a
# lone character as punctuation and, within a word-like segment (see
# default_segment's apostrophe rule), to find ASCII punctuation
# boundaries a segmenter merged into one word (e.g. "don't").
PUNCTUATION_REGEX = re.compile(r"[(){}\[\]<>.,;:'\"!?+\-=*/\\|&%^$#@~`]")

# CJK Unified Ideographs + common extension/compat blocks. Deliberately
# narrow (no Hiragana/Katakana/Hangul) — this only needs to be "reasonable",
# not exhaustive.
_CJK_RANGES = ((0x4E00, 0x9FFF), (0x3400, 0x4DBF), (0xF900, 0xFAFF))


def is_whitespace_char(s: str) -> bool:
    """Port of utils.ts isWhitespaceChar(): true if `s` CONTAINS
    whitespace anywhere (JS's unanchored /\\s/.test()), not that it's
    entirely whitespace. Only ever called here on single-character or
    pure-whitespace-run segments, so the distinction doesn't bite in
    practice — kept faithful to the source regardless."""
    return re.search(r"\s", s) is not None


@dataclass
class Segment:
    """Stand-in for the fields of Intl.SegmentData that word-navigation.ts
    actually reads (.segment, .isWordLike) — .index/.input are never
    used by findWordBackward/findWordForward so they're dropped here."""

    segment: str
    is_word_like: bool


def _is_cjk(ch: str) -> bool:
    cp = ord(ch)
    return any(lo <= cp <= hi for lo, hi in _CJK_RANGES)


def _is_word_char(ch: str) -> bool:
    return (ch.isalnum() or ch == "_") and not _is_cjk(ch)


def default_segment(text: str) -> list[Segment]:
    """Default UAX#29 word segmentation, no CJK dictionary (see module
    docstring). Word runs may absorb a single apostrophe flanked by
    word characters on both sides ("don't" -> one segment), matching
    Intl.Segmenter's MidLetter rule as observed against a real Node
    runtime; every other non-word, non-whitespace character is its own
    one-character segment, also matching observed Intl.Segmenter output
    (e.g. "foo...bar" -> three separate "." segments, never one "...").
    """
    segments: list[Segment] = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch.isspace():
            j = i + 1
            while j < n and text[j].isspace():
                j += 1
            segments.append(Segment(text[i:j], False))
            i = j
        elif _is_cjk(ch):
            segments.append(Segment(ch, True))
            i += 1
        elif _is_word_char(ch):
            j = i + 1
            while True:
                if j < n and _is_word_char(text[j]):
                    j += 1
                elif (
                    j + 1 < n
                    and text[j] == "'"
                    and _is_word_char(text[j - 1])
                    and _is_word_char(text[j + 1])
                ):
                    j += 1  # absorb the apostrophe, next loop turn absorbs the letter after it
                else:
                    break
            segments.append(Segment(text[i:j], True))
            i = j
        else:
            segments.append(Segment(ch, False))
            i += 1
    return segments


@dataclass
class WordNavigationOptions:
    """When omitted, findWordBackward/findWordForward use default_segment.

    segment: custom segmenter returning Segments for the given text.
    is_atomic_segment: predicate identifying segments that should be
    treated as single units (e.g. paste markers)."""

    segment: Callable[[str], Iterable[Segment]] | None = None
    is_atomic_segment: Callable[[str], bool] | None = None


def find_word_backward(text: str, cursor: int, options: WordNavigationOptions | None = None) -> int:
    """Cursor position after moving one word backward from `cursor` in
    `text`. Skips trailing whitespace, then stops at the next
    word/punctuation boundary.

    Pure function — does not mutate any state."""
    if cursor <= 0:
        return 0

    opts = options or WordNavigationOptions()
    is_atomic = opts.is_atomic_segment or (lambda _s: False)
    text_before_cursor = text[:cursor]
    segments = list((opts.segment or default_segment)(text_before_cursor))
    new_cursor = cursor

    while segments and not is_atomic(segments[-1].segment) and is_whitespace_char(segments[-1].segment):
        new_cursor -= len(segments.pop().segment)

    if not segments:
        return new_cursor

    last = segments[-1]

    if is_atomic(last.segment):
        new_cursor -= len(last.segment)
    elif last.is_word_like:
        # Stop right after the last embedded punctuation mark instead of
        # jumping over the whole segment — a segmenter may merge e.g.
        # "don't" into one word-like unit, but the cursor should still
        # stop at the apostrophe like it would for a plain word boundary.
        matches = list(PUNCTUATION_REGEX.finditer(last.segment))
        if not matches:
            new_cursor -= len(last.segment)
        else:
            new_cursor -= len(last.segment) - matches[-1].end()
    else:
        while (
            segments
            and not is_atomic(segments[-1].segment)
            and not segments[-1].is_word_like
            and not is_whitespace_char(segments[-1].segment)
        ):
            new_cursor -= len(segments.pop().segment)

    return new_cursor


def find_word_forward(text: str, cursor: int, options: WordNavigationOptions | None = None) -> int:
    """Cursor position after moving one word forward from `cursor` in
    `text`. Skips leading whitespace, then stops at the next
    word/punctuation boundary.

    Pure function — does not mutate any state."""
    if cursor >= len(text):
        return len(text)

    opts = options or WordNavigationOptions()
    is_atomic = opts.is_atomic_segment or (lambda _s: False)
    text_after_cursor = text[cursor:]
    iterator = iter((opts.segment or default_segment)(text_after_cursor))
    current = next(iterator, None)
    new_cursor = cursor

    while current is not None and not is_atomic(current.segment) and is_whitespace_char(current.segment):
        new_cursor += len(current.segment)
        current = next(iterator, None)

    if current is None:
        return new_cursor

    if is_atomic(current.segment):
        new_cursor += len(current.segment)
    elif current.is_word_like:
        # Stop right at the first embedded punctuation mark rather than
        # the end of the segment — mirrors find_word_backward's symmetric
        # handling of a segmenter-merged unit like "don't".
        match = PUNCTUATION_REGEX.search(current.segment)
        new_cursor += match.start() if match else len(current.segment)
    else:
        while (
            current is not None
            and not is_atomic(current.segment)
            and not current.is_word_like
            and not is_whitespace_char(current.segment)
        ):
            new_cursor += len(current.segment)
            current = next(iterator, None)

    return new_cursor
