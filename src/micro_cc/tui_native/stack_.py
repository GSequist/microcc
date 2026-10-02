"""Layout engine for nested containers: allocateStackSizes/distribute/clampSize,
plus a concrete VStack that calls them directly.

Implements a flexible sizing model where containers allocate available space to
their children according to basis (auto or fixed size), grow/shrink weights, and
min/max bounds. VStack renders by calling allocate_stack_sizes on itself directly;
the indirection layer for generic component dispatch is deferred until HStack or
ScrollView need it."""

import os
import sys
import time
import traceback
from dataclasses import dataclass
from typing import Any

MAX_SIZE = sys.maxsize  # stands in for TS's Number.MAX_SAFE_INTEGER / "no cap"

# Any single component's render() raising used to take the ENTIRE frame
# down with it: VStack.render()/render_in() called every entry's render()
# in one list comprehension, so one bad widget (a status line with
# malformed markup, a picker indexing past a list that just got emptied by
# /clear, ...) killed the whole composition — and since MicroTui._render_loop
# had no try/except of its own either (until a narrower fix for one such
# bug), that meant the screen just stopped updating forever with nothing
# on screen to explain why. _safe_render below is the actual fix for the
# whole CLASS of bug, not just the one instance that surfaced it: no
# widget's render() is ever allowed to prevent every OTHER widget from
# still drawing. A broken widget now just occupies zero rows this frame
# (see how its 0-length output flows into allocate_stack_sizes below)
# instead of corrupting or killing the entire frame.
_last_logged: dict[int, str] = {}  # id(component) -> last error signature logged


def _log_render_error(component) -> None:
    """Once per distinct failure per component, not once per frame (this
    can be hit up to ~30x/sec) — this is a shipped, pip-installed CLI with
    no live debugger attached, so a durable trace on disk is the only way
    a bug like this is ever diagnosable after the fact."""
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
    """Direct translation of stack.ts's StackEntry (component +
    StackEntryOptions). Defaults match stack.ts's addChild: grow=0,
    shrink=1, minSize=0, maxSize=MAX_SAFE_INTEGER (i.e. "uncapped")."""
    component: Any
    basis: object = "auto"        # "auto" or a fixed int
    grow: int = 0
    shrink: int = 1
    min_size: int = 0
    max_size: int = MAX_SIZE
    # No `size` field here — the allocate_stack_sizes algorithm never mutates
    # the entry; it returns a plain number[] the caller keeps separately
    # (see VStack.last_sizes below). An earlier version added a `size` field
    # with a comment claiming allocate_stack_sizes would write to it. Nothing
    # ever did. Caught by the demo script reading it and always getting 0.
    # Real lesson: don't add state that isn't computed or updated, it goes
    # stale silently.


def clamp_size(size: int, e: Entry) -> int:
    """stack.ts clampSize(): bound size into [min_size, max_size], and
    max_size itself is bounded below by min_size (so a misconfigured
    max < min can't produce a negative range)."""
    lo = max(0, e.min_size)
    hi = max(lo, e.max_size)
    return max(lo, min(hi, max(0, size)))


def distribute(sizes: list[int], entries: list[Entry], amount: int, mode: str) -> None:
    """Direct translation of stack.ts distribute(). mode is "grow" or
    "shrink". Note shrink's weight is NOT just entry.shrink, it's
    entry.shrink * max(1, current_size) — a bigger box gives up more
    space than a smaller one at the same shrink factor, matching
    flexbox-style shrink. Grow's weight is just entry.grow."""
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
    """Direct translation of stack.ts allocateStackSizes()."""
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
    """Concrete stack, satisfies the Component protocol (render/invalidate)
    so it can nest inside another VStack later. Wires the allocation math
    directly to alt_screen_.py's Component shape rather than going through
    a generic layout-node indirection layer (see module docstring)."""

    def __init__(self, gap: int = 0):
        self.gap = gap
        self.entries: list[Entry] = []
        # The sizes allocate_stack_sizes returned on the most recent
        # render/render_in call, index-aligned with self.entries. Added here
        # because something outside VStack (a debug readout, a status line)
        # legitimately needs to ask "how tall did child i actually end up"
        # after the fact. Recomputed every render call, never trust a stale
        # read from before the first render/render_in.
        self.last_sizes: list[int] = []
        # Row each entry's block of output starts at within this VStack's
        # own composed output, index-aligned with self.entries. Needed so
        # something outside (search-match highlighting) can translate
        # "row N inside this specific child's content" into "row N on the
        # actual screen" without this VStack needing to know why it's
        # being asked.
        self.last_offsets: list[int] = []

    def add(self, component, *, basis="auto", grow=0, shrink=1,
            min_size=0, max_size=MAX_SIZE) -> None:
        self.entries.append(Entry(component, basis, grow, shrink, min_size, max_size))

    def render(self, width: int) -> list[str]:
        rendered = [_safe_render(e.component, width) for e in self.entries]
        intrinsic = [len(lines) for lines in rendered]
        sizes = allocate_stack_sizes(self.entries, intrinsic, available_size=None, gap=self.gap)
        # available_size=None here means "just report clamped intrinsic
        # sizes, don't grow/shrink" — matches stack.ts when nested inside
        # another stack with no forced height. The *root* call site (in
        # alt_screen_'s render_layout_frame, once VStack is the real root)
        # is where a real terminal height gets passed in instead.
        self.last_sizes = sizes
        return self._compose(rendered, sizes)

    def render_in(self, width: int, height: int) -> list[str]:
        """Root-level render: same as render(), but with a real available
        height, so grow/shrink actually kick in. This is what
        render_layout_frame should call on the root component instead of
        plain render(width) once the root is a VStack."""
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
                # A child that manages its own scroll position (ScrollView)
                # doesn't want a plain top-truncate — it needs to pick which
                # scroll_top-relative window of its full content to show,
                # and update its own follow-end state for this frame's real
                # size. See scroll_view_.ScrollView.get_scrolled_lines.
                #
                # get_scrolled_lines can itself raise independently of
                # `lines` (already safely rendered above) — it does its own
                # work (update_layout, kitty-image span slicing). Same
                # fault-isolation as _safe_render: don't let it take the
                # whole frame down, just pad/truncate plainly this frame.
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
        """Drop the entry wrapping `component`, if present. No-op if it
        isn't in this stack — mirrors Textual's Widget.remove() being safe
        to call on an already-unmounted widget, which callers like
        _finalize_streaming rely on."""
        self.entries = [e for e in self.entries if e.component is not component]

    def replace(self, old_component, new_component, *, basis="auto", grow=0,
                shrink=1, min_size=0, max_size=MAX_SIZE) -> None:
        """Swap `new_component` into the exact slot `old_component`
        currently occupies (same index, same surrounding layout order) —
        the native equivalent of the old Textual app's picker.display =
        True: one widget slot, a different thing made visible in it, NOT
        a floating overlay. Used for the ask-user question UI, which sits
        in bottom_bar between the same two HRules the prompt normally
        occupies, rather than the show_overlay()/anchor="center" path
        every other picker uses — that path is right for a true modal,
        wrong here (it was rendering the question overlay somewhere in
        the middle of the screen instead of in the input's own slot, and
        clamping its height to the overlay's max_height instead of
        letting it size to its own real content, same as the old
        Textual layout did)."""
        for i, e in enumerate(self.entries):
            if e.component is old_component:
                self.entries[i] = Entry(new_component, basis, grow, shrink, min_size, max_size)
                return
        raise ValueError("replace(): old_component not found in this stack")

    def clear(self) -> None:
        self.entries = []

    def find_offset(self, component) -> int | None:
        """Where does `component`'s own block start in this VStack's most
        recent composed output? Walks one level into any nested VStack
        too (adding that child's own start row to whatever offset it
        reports), so a ScrollView buried inside a nested VStack is still
        findable from the root. Returns None if `component` isn't
        anywhere in this tree."""
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
        """The inverse of find_offset: given a row LOCAL to this VStack's
        most recent composed output (0 = this stack's own first row),
        which leaf component's block contains it? Recurses into any child
        that also has find_component_at (a nested VStack, or a ScrollView
        — see scroll_view_.ScrollView.find_component_at, which translates
        viewport-relative rows to content-relative ones before doing the
        same lookup on what it wraps), so this is the real hit-test:
        the piece Textual's own widget-geometry tracking used to give for
        free, needed for anything mouse-click-driven (see
        MessageRow.toggle_expanded's caller in start_live_tui_.py).
        Returns None if `row` doesn't land inside any entry."""
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

