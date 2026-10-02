"""ScrollView: a component that wraps another component and shows only a
scrollTop-relative window of its rendered output, with a real state
machine for "stay pinned to the bottom as content grows" (follow-end /
sticky scroll) instead of a bare on/off flag.

Layout-model note: a real terminal-UI layout engine keeps content and
viewport as separate rectangles with independent screen positions, and
"windowing" happens by translating the content rectangle upward by
scrollTop and letting a clip rectangle hide whatever falls outside the
viewport. This module doesn't have that rectangle/clip tree (stack_.py's
VStack works in flat lists of strings, not positioned boxes), so it gets
the same *effect* the direct way: render the child's full content, then
slice out exactly `viewport_height` lines starting at `scroll_top`. Same
visible result, no rectangle math. Revisit if a future feature needs a
child positioned at an arbitrary (x, y) independent of stack order (a
floating widget, non-VStack layout) — that's when the rectangle/clip
version actually earns its complexity.
"""

from dataclasses import dataclass

from micro_cc.tui_native.detect_images_ import find_kitty_image_spans, crop_kitty_image_line


@dataclass
class ScrollToOptions:
    disable_follow: bool = False


class ScrollView:
    def __init__(self, child, *, follow: str = "none", primary: bool = False, overscroll: str = "chain"):
        if follow not in ("none", "end"):
            raise ValueError(f"unsupported follow mode: {follow!r}")
        self.child = child
        self._follow_end = follow == "end"
        self._following_end = self._follow_end
        self._follow_suppressed_at_end = False
        self.primary = primary
        self.overscroll = overscroll
        self._scroll_top = 0
        self._content_height = 0
        self._viewport_height = 0

    # --- read-only state, mirrors what a caller needs to draw a scrollbar
    # or decide "should I keep auto-scrolling" -------------------------
    @property
    def scroll_top(self) -> int:
        return self._scroll_top

    @property
    def is_following_end(self) -> bool:
        return self._following_end

    @property
    def viewport_height(self) -> int:
        return self._viewport_height

    @property
    def content_height(self) -> int:
        return self._content_height

    @property
    def max_scroll_top(self) -> int:
        return max(0, self._content_height - self._viewport_height)

    # --- Component protocol --------------------------------------------
    def render(self, width: int) -> list[str]:
        """Full, unwindowed content — the same thing update_layout wants
        as its content_height input. Whoever places this ScrollView inside
        a fixed-height slot (VStack, currently, via get_scrolled_lines) is
        responsible for cutting this down to the visible window; render()
        alone doesn't know its own allocated height."""
        return self.child.render(width)

    def invalidate(self) -> None:
        self.child.invalidate()

    # --- the windowing hook VStack looks for -----------------------------
    def get_scrolled_lines(self, full_lines: list[str], viewport_height: int) -> list[str]:
        """Called once per frame by whatever allocated this ScrollView a
        height (VStack._compose looks for this method by name on every
        child). Updates scroll state for this frame's real numbers, then
        returns exactly viewport_height lines starting at the resulting
        scroll_top, padded with empty lines if content is shorter than
        the viewport."""
        self.update_layout(len(full_lines), viewport_height)
        top = self._scroll_top
        window = full_lines[top: top + viewport_height]
        if len(window) < viewport_height:
            window = window + [""] * (viewport_height - len(window))
        # A Kitty image's escape sequence lives on only the first of its
        # several reserved rows (see detect_images_.crop_kitty_image_line's
        # docstring) — a plain slice either includes that line whole or
        # drops it whole, so scrolling the window's top edge into the
        # middle of an image made it disappear entirely rather than
        # partially. Checking `window` here (instead of full_lines) would
        # defeat the entire fix: the exact case this handles is the escape
        # sequence line having already scrolled out of the naive slice —
        # window legitimately has zero "\x1b_G" occurrences in that case
        # even though some of the image's padding rows are still in it.
        if any("\x1b_G" in line for line in full_lines):
            for start, row_count in find_kitty_image_spans(full_lines):
                end = start + row_count
                if end <= top or start >= top + viewport_height:
                    continue   # fully outside the window
                if start >= top and end <= top + viewport_height:
                    continue   # fully inside — the plain slice above is already correct
                hidden_rows = max(0, top - start)
                visible_rows = min(row_count - hidden_rows, viewport_height - max(0, start - top))
                window_idx = max(0, start - top)
                if 0 <= window_idx < len(window):
                    window[window_idx] = crop_kitty_image_line(full_lines[start], hidden_rows, visible_rows)
        return window

    def find_component_at(self, row: int):
        """`row` here is viewport-relative (0 = the first visible row on
        screen) — VStack.find_component_at hands it in exactly that shape
        for any child it finds this method on. Translate to content-
        relative (scroll_top + row) before delegating to whatever's
        wrapped, since that's the coordinate space the child's own offsets
        were computed in. Out of the viewport (past what update_layout
        last saw) returns None rather than guessing."""
        if not (0 <= row < self._viewport_height):
            return None
        content_row = self._scroll_top + row
        child_find = getattr(self.child, "find_component_at", None)
        if child_find is not None:
            return child_find(content_row)
        return self.child

    def update_layout(self, content_height: int, viewport_height: int) -> None:
        """Recompute scroll_top/following_end for this frame's real
        content/viewport sizes. Order of operations matters here (this is
        the actual sticky-scroll fix, not a bare flag):
          1. Clamp content/viewport to non-negative.
          2. If we were following the end last frame, snap to the new
             bottom unconditionally — this is what makes newly streamed-in
             content keep the view pinned down instead of leaving it stuck
             wherever the old max_scroll_top used to be.
          3. Otherwise just re-clamp the existing scroll_top into range
             (viewport got bigger/smaller, or content shrank).
          4. If that clamp put us strictly above the new bottom, any
             earlier "don't re-engage follow" suppression no longer
             applies — the user scrolling back up and content then
             shrinking underneath them shouldn't leave a stale suppression
             flag lying around forever.
          5. Only *re-arm* follow-end (when it was off) if we're now
             exactly at the bottom AND nothing explicitly suppressed that
             (see scroll_to's disable_follow) — landing on the last line by
             coincidence during a manual scroll shouldn't silently turn
             sticky-scroll back on underneath the user.
        """
        self._content_height = max(0, content_height)
        self._viewport_height = max(0, viewport_height)
        max_top = self.max_scroll_top
        if self._following_end:
            self._scroll_top = max_top
        else:
            self._scroll_top = max(0, min(self._scroll_top, max_top))
        if self._scroll_top < max_top:
            self._follow_suppressed_at_end = False
        if self._follow_end and self._scroll_top == max_top and not self._follow_suppressed_at_end:
            self._following_end = True

    # --- scroll commands --------------------------------------------------
    def scroll_to(self, scroll_top: int, options: ScrollToOptions | None = None) -> None:
        opts = options or ScrollToOptions()
        max_top = self.max_scroll_top
        next_top = max(0, min(max_top, int(scroll_top)))
        next_suppressed = opts.disable_follow and next_top == max_top
        next_following = (not next_suppressed) and self._follow_end and next_top == max_top
        self._scroll_top = next_top
        self._following_end = next_following
        self._follow_suppressed_at_end = next_suppressed

    def scroll_by(self, lines: int) -> int:
        """Move by `lines` (negative = up), clamped to content bounds.
        Returns the leftover amount that didn't fit (0 if it all applied)
        — lets a caller chain the remainder into an outer scrollable
        region once nested scroll areas exist."""
        requested = int(lines)
        if requested == 0:
            return 0
        max_top = self.max_scroll_top
        start = max_top if self._following_end else self._scroll_top
        next_top = max(0, min(max_top, start + requested))
        moved = next_top - start
        self._scroll_top = next_top
        self._following_end = self._follow_end and next_top == max_top
        self._follow_suppressed_at_end = False
        return requested - moved

    def scroll_to_start(self) -> None:
        self._scroll_top = 0
        # Only re-arm follow-end if there's nothing to scroll at all — if
        # there's real content below the viewport, jumping to the top is
        # explicitly "I want to read from here", not "resume following".
        self._following_end = self._follow_end and self._content_height <= self._viewport_height
        self._follow_suppressed_at_end = False

    def scroll_to_end(self) -> None:
        self._scroll_top = self.max_scroll_top
        self._following_end = self._follow_end
        self._follow_suppressed_at_end = False
