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

