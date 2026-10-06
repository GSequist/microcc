"""ScrollView wraps a component with windowing and follow-end (sticky-scroll) state."""

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
        """Return full, unwindowed content; caller handles windowing via get_scrolled_lines."""
        return self.child.render(width)

    def invalidate(self) -> None:
        self.child.invalidate()

    # --- the windowing hook VStack looks for -----------------------------
    def get_scrolled_lines(self, full_lines: list[str], viewport_height: int) -> list[str]:
        """Update scroll state and return viewport_height lines starting at scroll_top."""
        self.update_layout(len(full_lines), viewport_height)
        top = self._scroll_top
        window = full_lines[top: top + viewport_height]
        if len(window) < viewport_height:
            window = window + [""] * (viewport_height - len(window))
        # Kitty image escape sequence lives on first row only; crop partial windows.
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
        """Find component at viewport-relative row; translate to content-relative."""
        if not (0 <= row < self._viewport_height):
            return None
        content_row = self._scroll_top + row
        child_find = getattr(self.child, "find_component_at", None)
        if child_find is not None:
            return child_find(content_row)
        return self.child

    def update_layout(self, content_height: int, viewport_height: int) -> None:
        """Recompute scroll_top/following_end; snap to bottom if following, else re-clamp."""
        # Order of operations: clamp, snap if following, clamp suppression, re-arm on-bottom.
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
        """Move by lines (negative = up); return leftover that didn't fit."""
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
        # Re-arm follow-end only if content fits viewport (nothing to scroll).
        self._following_end = self._follow_end and self._content_height <= self._viewport_height
        self._follow_suppressed_at_end = False

    def scroll_to_end(self) -> None:
        self._scroll_top = self.max_scroll_top
        self._following_end = self._follow_end
        self._follow_suppressed_at_end = False
