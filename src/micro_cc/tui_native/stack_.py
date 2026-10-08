"""Layout engine: flexible sizing model with grow/shrink and bounds; concrete VStack."""

import os
import sys
import time
import traceback
from dataclasses import dataclass
from typing import Any

MAX_SIZE = sys.maxsize  # "no cap"

# _safe_render isolates render() failures; one broken widget doesn't kill the whole frame.
_last_logged: dict[int, str] = {}  # id(component) -> last error signature logged


def _log_render_error(component) -> None:
    """Log error once per distinct failure per component (deduped to 1/sec max)."""
    sig = traceback.format_exc()
    if _last_logged.get(id(component)) == sig:
        return
    _last_logged[id(component)] = sig
    try:
        log_path = os.path.expanduser("~/.micro-cc/render_errors.log")
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        with open(log_path, "a") as f:
            f.write(f"--- {time.strftime('%Y-%m-%d %H:%M:%S')} {type(component).__name__} ---\n")
            f.write(sig)
            f.write("\n")
    except OSError:
        pass


def _safe_render(component, width: int) -> list[str]:
    try:
        return component.render(width)
    except Exception:
        _log_render_error(component)
        return []


@dataclass
class Entry:
    """Stack entry: component + sizing options."""
    component: Any
    basis: object = "auto"        # "auto" or a fixed int
    grow: int = 0
    shrink: int = 1
    min_size: int = 0
    max_size: int = MAX_SIZE
    # No size field; allocate_stack_sizes returns sizes separately (VStack.last_sizes).


def clamp_size(size: int, e: Entry) -> int:
    """Bound size into [min_size, max_size]; max is bounded below by min."""
    lo = max(0, e.min_size)
    hi = max(lo, e.max_size)
    return max(lo, min(hi, max(0, size)))


def distribute(sizes: list[int], entries: list[Entry], amount: int, mode: str) -> None:
    """Distribute size by grow/shrink weights; shrink is flexbox-style (by current size)."""
    remaining = amount
    while remaining > 0:
        candidates = []
        for i, e in enumerate(entries):
            if mode == "grow":
                if e.grow > 0 and sizes[i] < e.max_size:
                    candidates.append(i)
            else:
                if e.shrink > 0 and sizes[i] > e.min_size:
                    candidates.append(i)
        if not candidates:
            return

        def weight(i: int) -> int:
            e = entries[i]
            if mode == "grow":
                return e.grow
            return e.shrink * max(1, sizes[i])

        total_weight = sum(weight(i) for i in candidates)
        distributed = 0
        for i in candidates:
            if remaining <= 0:
                break
            e = entries[i]
            proposed = max(1, (remaining * weight(i)) // total_weight)
            capacity = (e.max_size - sizes[i]) if mode == "grow" else (sizes[i] - e.min_size)
            delta = min(remaining, proposed, capacity)
            if delta <= 0:
                continue
            sizes[i] += delta if mode == "grow" else -delta
            remaining -= delta
            distributed += delta
        if distributed == 0:  # deadlock guard
            return


def allocate_stack_sizes(entries: list[Entry], intrinsic_sizes: list[int],
                          available_size: int | None, gap: int) -> list[int]:
    """Allocate sizes across stack entries using grow/shrink distribution."""
    sizes = [
        clamp_size(intrinsic_sizes[i] if e.basis == "auto" else e.basis, e)
        for i, e in enumerate(entries)
    ]
    if available_size is None:
        return sizes
    content_size = max(0, available_size - max(0, len(entries) - 1) * gap)
    total = sum(sizes)
    if total < content_size:
        distribute(sizes, entries, content_size - total, "grow")
    elif total > content_size:
        distribute(sizes, entries, total - content_size, "shrink")
    return sizes


class VStack:
    """Concrete vertical stack; satisfies Component protocol for nesting."""

    def __init__(self, gap: int = 0):
        self.gap = gap
        self.entries: list[Entry] = []
        # Sizes from most recent render; recomputed each call.
        self.last_sizes: list[int] = []
        # Row offsets of each entry's output in this VStack's composed output.
        self.last_offsets: list[int] = []

    def add(self, component, *, basis="auto", grow=0, shrink=1,
            min_size=0, max_size=MAX_SIZE) -> None:
        self.entries.append(Entry(component, basis, grow, shrink, min_size, max_size))

    def render(self, width: int) -> list[str]:
        rendered = [_safe_render(e.component, width) for e in self.entries]
        intrinsic = [len(lines) for lines in rendered]
        sizes = allocate_stack_sizes(self.entries, intrinsic, available_size=None, gap=self.gap)
        # available_size=None: clamped intrinsic sizes, no grow/shrink. Root passes height.
        self.last_sizes = sizes
        return self._compose(rendered, sizes)

    def render_in(self, width: int, height: int) -> list[str]:
        """Root-level render with real height; grow/shrink kick in."""
        rendered = [_safe_render(e.component, width) for e in self.entries]
        intrinsic = [len(lines) for lines in rendered]
        sizes = allocate_stack_sizes(self.entries, intrinsic, available_size=height, gap=self.gap)
        self.last_sizes = sizes
        return self._compose(rendered, sizes)

    def _compose(self, rendered: list[list[str]], sizes: list[int]) -> list[str]:
        out: list[str] = []
        offsets: list[int] = []
        for i, lines in enumerate(rendered):
            h = sizes[i]
            offsets.append(len(out))
            component = self.entries[i].component
            get_scrolled_lines = getattr(component, "get_scrolled_lines", None)
            if get_scrolled_lines is not None:
                # ScrollView picks its own scroll window; treat separately from plain truncation.
                try:
                    lines = get_scrolled_lines(lines, h)
                except Exception:
                    _log_render_error(component)
                    lines = (lines + [""] * h)[:h]
            elif len(lines) < h:
                lines = lines + [""] * (h - len(lines))
            else:
                lines = lines[:h]
            out.extend(lines)
            if i < len(rendered) - 1 and self.gap:
                out.extend([""] * self.gap)
        self.last_offsets = offsets
        return out

    def remove(self, component) -> None:
        """Drop the entry wrapping component; safe to call if not present."""
        self.entries = [e for e in self.entries if e.component is not component]

    def replace(self, old_component, new_component, *, basis="auto", grow=0,
                shrink=1, min_size=0, max_size=MAX_SIZE) -> None:
        """Swap new_component into old_component's exact slot; not a floating overlay."""
        for i, e in enumerate(self.entries):
            if e.component is old_component:
                self.entries[i] = Entry(new_component, basis, grow, shrink, min_size, max_size)
                return
        raise ValueError("replace(): old_component not found in this stack")

    def clear(self) -> None:
        self.entries = []

    def find_offset(self, component) -> int | None:
        """Return row where component's block starts in composed output; None if not found."""
        for i, entry in enumerate(self.entries):
            if entry.component is component:
                return self.last_offsets[i]
            nested_find = getattr(entry.component, "find_offset", None)
            if nested_find is not None:
                nested = nested_find(component)
                if nested is not None:
                    return self.last_offsets[i] + nested
        return None

    def find_component_at(self, row: int):
        """Return leaf component containing row in composed output; None if no match."""
        for i, entry in enumerate(self.entries):
            start = self.last_offsets[i]
            height = self.last_sizes[i] if i < len(self.last_sizes) else 0
            if start <= row < start + height:
                child = entry.component
                nested_find = getattr(child, "find_component_at", None)
                if nested_find is not None:
                    found = nested_find(row - start)
                    return found if found is not None else child
                return child
        return None

    def invalidate(self) -> None:
        for e in self.entries:
            e.component.invalidate()



def _keep_images(left: str, out: str, left_w: int) -> str:
    """Re-attach the kitty image escapes compositing strips, at their original column (CHA, zero-width)."""
    start = left.find("\x1b_G")
    if start == -1:
        return out
    from micro_cc.tui_native.text_utils_ import visible_width
    end = left.rfind("\x1b\\") + 2  # chunked transmits are one contiguous run
    c = visible_width(left[:start])
    if c >= left_w:
        return out
    seq = left[start:end]
    return seq + out if c == 0 else f"\x1b[{c + 1}G{seq}\x1b[1G{out}"


class HSplit:
    """Conversation plus an optional mod pane on its right, top or bottom; collapses to the conversation alone when too small or closed."""

    MIN_LEFT = 60     # conversation columns kept beside a right pane
    MIN_PANE = 24     # narrowest right pane
    MIN_ROWS = 6      # conversation rows kept beside a top/bottom pane
    MIN_PANE_ROWS = 3

    def __init__(self, left):
        self.left = left
        self.pane = None           # callable(width, height) -> lines, or None when the pane closed itself
        self.pane_width = "40%"    # size: columns for "right", rows for "top"/"bottom"; int or "NN%"
        self.side = "right"        # "right" | "top" | "bottom"
        self.on_close = None       # called when pane returns None
        self.rect = None           # (col, row, width, height) of the pane in the last frame; None = not shown
        self._width = 0
        self._pane_w = 0
        self._left_top = 0         # rows above the conversation (top pane + divider)
        self._left_lines: list[str] = []

    @staticmethod
    def _size(spec, total: int) -> int:
        return total * int(spec[:-1]) // 100 if isinstance(spec, str) and spec.endswith("%") else int(spec)

    def _pane_cols(self, width: int) -> int:
        if self.pane is None or self.side != "right":
            return 0
        cols = min(self._size(self.pane_width, width), width - self.MIN_LEFT - 1)
        return cols if cols >= self.MIN_PANE else 0

    def _pane_rows(self, height: int) -> int:
        rows = min(self._size(self.pane_width, height), height - self.MIN_ROWS - 1)
        return rows if rows >= self.MIN_PANE_ROWS else 0

    def render(self, width: int) -> list[str]:
        self._width, self._pane_w = width, self._pane_cols(width)
        left_w = width - self._pane_w - 1 if self._pane_w else width
        self._left_lines = _safe_render(self.left, left_w)
        return self._left_lines

    def _scroll_left(self, height: int) -> list[str]:
        scrolled = getattr(self.left, "get_scrolled_lines", None)
        return scrolled(self._left_lines, height) if scrolled else (self._left_lines + [""] * height)[:height]

    def _draw_pane(self, w: int, h: int):
        """Pane lines padded to h, or None after closing a pane that returned None."""
        try:
            lines = self.pane(w, h)
        except Exception:
            _log_render_error(self.pane)
            lines = []
        if lines is None:
            self.pane, self.rect = None, None
            if self.on_close:
                self.on_close()
            return None
        return (list(lines) + [""] * h)[:h]

    def get_scrolled_lines(self, full_lines: list[str], height: int) -> list[str]:
        self._left_top = 0
        if self.pane is not None and self.side in ("top", "bottom"):
            return self._vertical(height)
        lines = self._scroll_left(height)
        if not self._pane_w:
            self.rect = None
            return lines
        from micro_cc.tui_native.alt_screen_ import composite_tui_line
        right = self._draw_pane(self._pane_w, height)
        if right is None:
            return lines
        col = self._width - self._pane_w
        self.rect = (col, 0, self._pane_w, height)
        bar = "\x1b[2m│\x1b[22m"
        return [_keep_images(l, composite_tui_line(l, bar + r, col - 1, self._pane_w + 1, self._width), col - 1)
                for l, r in zip(lines, right)]

    def _vertical(self, height: int) -> list[str]:
        ph = self._pane_rows(height)
        if not ph:
            self.rect = None
            return self._scroll_left(height)
        left_h = height - ph - 1
        pane = self._draw_pane(self._width, ph)
        if pane is None:
            return self._scroll_left(height)
        rule = "\x1b[2m" + "─" * self._width + "\x1b[22m"
        if self.side == "top":
            self.rect, self._left_top = (0, 0, self._width, ph), ph + 1
            return pane + [rule] + self._scroll_left(left_h)
        self.rect = (0, left_h + 1, self._width, ph)
        return self._scroll_left(left_h) + [rule] + pane

    def find_offset(self, component) -> int | None:
        if component is self.left:
            return self._left_top
        nested = getattr(self.left, "find_offset", None)
        found = nested(component) if nested else None
        return None if found is None else self._left_top + found

    def find_component_at(self, row: int):
        row -= self._left_top  # pane/divider rows land outside the conversation's viewport
        nested = getattr(self.left, "find_component_at", None)
        return nested(row) if nested else self.left

    def invalidate(self) -> None:
        self.left.invalidate()
