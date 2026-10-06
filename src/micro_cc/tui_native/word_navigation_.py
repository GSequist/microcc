"""Word navigation with UAX#29 segmentation; default rules, no CJK dictionary."""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass

# Classify character as punctuation and find punctuation boundaries in merged words.
PUNCTUATION_REGEX = re.compile(r"[(){}\[\]<>.,;:'\"!?+\-=*/\\|&%^$#@~`]")

# CJK Unified Ideographs + common extensions; deliberately narrow (reasonable, not exhaustive).
_CJK_RANGES = ((0x4E00, 0x9FFF), (0x3400, 0x4DBF), (0xF900, 0xFAFF))


def is_whitespace_char(s: str) -> bool:
    """True if s contains whitespace anywhere (not entirely whitespace)."""
    return re.search(r"\s", s) is not None


@dataclass
class Segment:
    """Text run plus a flag marking it word-like."""

    segment: str
    is_word_like: bool


def _is_cjk(ch: str) -> bool:
    cp = ord(ch)
    return any(lo <= cp <= hi for lo, hi in _CJK_RANGES)


def _is_word_char(ch: str) -> bool:
    return (ch.isalnum() or ch == "_") and not _is_cjk(ch)


def default_segment(text: str) -> list[Segment]:
    """Default UAX#29 segmentation; apostrophes absorbed into word runs."""
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
    """Options: custom segment function and atomic segment predicate."""

    segment: Callable[[str], Iterable[Segment]] | None = None
    is_atomic_segment: Callable[[str], bool] | None = None


def find_word_backward(text: str, cursor: int, options: WordNavigationOptions | None = None) -> int:
    """Move one word backward from cursor; skip trailing whitespace."""
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
        # Stop at embedded punctuation (e.g. apostrophe in "don't").
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
    """Move one word forward from cursor; skip leading whitespace."""
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
        # Stop at first embedded punctuation (mirror of find_word_backward).
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
