import re
import shutil
import sys
from dataclasses import dataclass
from typing import Protocol

from micro_cc.tui_native.text_utils_ import (
    extract_ansi_code,
    extract_segments,
    get_osc8_link_at_column,
    slice_by_column,
    slice_with_width,
    strip_terminal_sequences,
    visible_width,
)


class Component(Protocol):
    """Anything with these two methods can sit in the widget tree.
    render(width) returns its content as a list of lines. invalidate()
    clears any cached render state so the next render is computed fresh."""
    def render(self, width: int) -> list[str]: ...
    def invalidate(self) -> None: ...


BEGIN_SYNCHRONIZED_OUTPUT = "\x1b[?2026h"  # one atomic terminal paint starts
END_SYNCHRONIZED_OUTPUT = "\x1b[?2026l"    # ...and ends here

# The alt screen also sets the terminal's own DEFAULT background/foreground
# (OSC 11/10) from the active theme set. Doing it here rather than painting a
# background per rendered line is what themes the whole UI with no renderer
# changes: every span that doesn't set its own bgcolor (body text, padding,
# dividers) inherits this default, and every widget that DOES set one keeps
# working. OSC 111/110 on exit restore the user's own terminal profile — the
# alt screen already restores its contents.
#
# Both sequences are FUNCTIONS, not constants: a /theme switch has to be able
# to re-emit them mid-session (see reapply_default_colors below), so binding
# them once at import would freeze the ground for the process's whole life.
def _enter() -> str:
    from micro_cc.utils import theme_store_
    return (
        "\x1b[?1049h"
        f"\x1b]11;{theme_store_.get('bg')}\x07"
        f"\x1b]10;{theme_store_.get('fg')}\x07"
        "\x1b[2J"
    )


def _exit() -> str:
    return "\x1b]111\x07\x1b]110\x07\x1b[?1049l"


def default_colors_sequence() -> str:
    """Just the OSC 11/10 pair, no alt-screen switch — what a live theme
    change re-emits to repaint the ground under an already-running UI."""
    from micro_cc.utils import theme_store_
    return (
        f"\x1b]11;{theme_store_.get('bg')}\x07"
        f"\x1b]10;{theme_store_.get('fg')}\x07"
    )


ENTER_ALT_SCREEN = _enter()
EXIT_ALT_SCREEN = _exit()

# A focused component emits this at the exact spot in its own render()
# output where the hardware cursor should land. extract_cursor_position
# finds it, strips it, and returns (row, col).
CURSOR_MARKER = "\x1b_pi:c\x07"

# Leading OSC 133 shell-integration markers (prompt/output-start/end) some
# shells prepend to prompt lines. Only strips a leading run of these.
_OSC133_ZONE_PREFIX = re.compile(r"^(?:\x1b\]133;[ABC](?:\x07|\x1b\\))+")

SEGMENT_RESET = "\x1b[0m\x1b]8;;\x07"


def render_layout_frame(root: Component, width: int, height: int) -> list[str]:
    """Render root at exactly `height` rows. If root supports render_in
    (real height-aware layout, e.g. VStack), use it. Otherwise render
    plain and pad/truncate to height."""
    render_in = getattr(root, "render_in", None)
    if render_in is not None:
        return render_in(width, height)
    lines = root.render(width)
    if len(lines) < height:
        lines = lines + [""] * (height - len(lines))
    else:
        lines = lines[:height]
    return lines


def strip_osc133_zone(line: str) -> str:
    return _OSC133_ZONE_PREFIX.sub("", line)


_KITTY_ID_RE = re.compile(r"(?:^|,)i=(\d+)(?:,|$)")


def extract_kitty_image_ids(screen: list[str]) -> set[int]:
    """Which Kitty image ids are actually present in this composed frame.
    Every image line's first control sequence carries i= — either the
    delete-before-transmit prefix or the transmit's own first chunk (see
    detect_images_.encode_kitty) — so finding just the first `\\x1b_G...;`
    on a line is enough, no need to scan every chunk of a multi-chunk
    transmission. str.find, not regex-over-the-whole-line, for the same
    reason extract_ansi_code was rewritten: a manual/heavier scan here
    would cost real time on a line that can be hundreds of KB."""
    ids = set()
    for line in screen:
        start = line.find("\x1b_G")
        if start == -1:
            continue
        end = line.find(";", start)
        if end == -1:
            continue
        m = _KITTY_ID_RE.search(line, start, end)
        if m:
            ids.add(int(m.group(1)))
    return ids


def composite_tui_line(base_line: str, overlay_line: str, start_col: int, overlay_width: int, total_width: int) -> str:
    """Stitch overlay_line into base_line at column start_col, keeping
    whatever of base_line survives before/after it. Used by both flashes
    and overlays to paint a fixed-position patch onto an existing line."""
    after_start = start_col + overlay_width
    before, before_width, after, after_width = extract_segments(
        base_line, start_col, after_start, total_width - after_start, True,
    )
    overlay_text, overlay_w = slice_with_width(overlay_line, 0, overlay_width, True)
    before_pad = max(0, start_col - before_width)
    overlay_pad = max(0, overlay_width - overlay_w)
    actual_before_width = max(start_col, before_width)
    actual_overlay_width = max(overlay_width, overlay_w)
    after_target = max(0, total_width - actual_before_width - actual_overlay_width)
    after_pad = max(0, after_target - after_width)
    result = (
        before
        + " " * before_pad
        + SEGMENT_RESET
        + overlay_text
        + " " * overlay_pad
        + SEGMENT_RESET
        + after
        + " " * after_pad
    )
    return result if visible_width(result) <= total_width else slice_by_column(result, 0, total_width, True)


@dataclass
class SearchMatch:
    """One occurrence of the search query. row/columns are in the scroll
    view's content coordinates, not screen coordinates — screen position
    is recomputed every frame from scroll_top + on-screen offset."""
    row: int
    start_col: int
    end_col: int


def composite_overlays(screen: list[str], width: int, height: int, overlay_stack: "list[OverlayEntry]") -> list[str]:
    """Stack every visible overlay onto `screen`, lowest focus_order first
    so newer overlays paint over older ones. Each overlay may want to sit
    lower than `screen` currently has lines for (e.g. anchored near the
    bottom of a short screen); rather than clip those, grow a scratch
    copy tall enough to hold every overlay at its real row, composite
    into that, then keep only the bottom `height` rows of it — so a
    too-low anchor slides the overlay up onto real screen rows instead
    of getting cut off."""
    visible = [e for e in overlay_stack if not e.hidden]
    if not visible:
        return screen
    result = list(screen)
    rendered = []
    min_lines_needed = len(result)
    for entry in sorted(visible, key=lambda e: e.focus_order):
        w, _, _, max_height = entry.layout(width, height)
        overlay_lines = entry.component.render(w)
        if max_height is not None and len(overlay_lines) > max_height:
            overlay_lines = overlay_lines[:max_height]
        _, row, col, _ = entry.layout(width, height, len(overlay_lines))
        rendered.append((overlay_lines, row, col, w))
        min_lines_needed = max(min_lines_needed, row + len(overlay_lines))

    working_height = max(len(result), height, min_lines_needed)
    while len(result) < working_height:
        result.append("")
    viewport_start = max(0, working_height - height)

    for overlay_lines, row, col, w in rendered:
        for i, overlay_line in enumerate(overlay_lines):
            idx = viewport_start + row + i
            if 0 <= idx < len(result):
                truncated = overlay_line if visible_width(overlay_line) <= w else slice_by_column(overlay_line, 0, w, True)
                result[idx] = composite_tui_line(result[idx], truncated, col, w, width)
    return result[viewport_start:] if viewport_start else result


def clip_overwide_line(line: str, width: int) -> str:
    """Truncate a line to `width` columns. Known gap: visible_width counts
    1 cell per character, no wide-CJK/combining-mark table yet — fine for
    ASCII, wrong once double-width glyphs actually appear on screen."""
    return slice_by_column(line, 0, width) if visible_width(line) > width else line


def apply_line_resets(line: str) -> str:
    """Stop one line's color/style from bleeding into the next line the
    terminal happens to reuse."""
    return line + SEGMENT_RESET


# --- Flashes -----------------------------------------------------------
# A transient inverse-video banner ("Copied!") composited over the bottom
# of the screen for a fixed duration. flash() records a wall-clock expiry;
# expire(), called once per do_render(), sweeps anything past it — no
# timer thread needed since this renderer already redraws every frame.
import time as _time


@dataclass
class _FlashEntry:
    message: str
    expires_at: float


class AltScreenFlashContainer:
    def __init__(self):
        self._entries: list[_FlashEntry] = []

    def flash(self, message: str, duration_ms: float = 1000) -> None:
        self._entries.append(_FlashEntry(message, _time.monotonic() + max(0, duration_ms) / 1000))

    def expire(self) -> None:
        now = _time.monotonic()
        self._entries = [e for e in self._entries if e.expires_at > now]

    def dispose(self) -> None:
        self._entries.clear()

    def render(self, width: int) -> list[str]:
        out = []
        for e in self._entries:
            text = f" {e.message} "
            if visible_width(text) > width:
                text = slice_by_column(text, 0, width)
            out.append(f"\x1b[7m{text}\x1b[27m")
        return out


def composite_flashes(screen: list[str], width: int, height: int, flashes: AltScreenFlashContainer) -> list[str]:
    flashes.expire()
    flash_lines = flashes.render(width)[-height:] if height else []
    if not flash_lines:
        return screen
    result = list(screen)
    while len(result) < height:
        result.append("")
    for row, line in enumerate(flash_lines):
        flash_width = visible_width(line)
        if flash_width == 0:
            continue
        result[row] = composite_tui_line(result[row] if row < len(result) else "", line, width - flash_width, flash_width, width)
    return result


# --- Overlays ------------------------------------------------------------
# Floating boxes (popups, pickers) positioned over the base screen at an
# anchor (corner/edge/center) plus optional explicit row/col/offset.
# Missing piece, tracked, not built: when overlay N closes, keyboard focus
# should return to whatever was focused before it opened, walking back
# through a whole stack of overlays if several are open. hide()/show()
# here only toggle render-visibility. Needed once keystroke routing to a
# focused component exists (Phase 4/5) and more than one overlay can be
# open at once — neither is true yet.
def _parse_size_value(value, reference: int):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    m = re.match(r"^(\d+(?:\.\d+)?)%$", value)
    if m:
        return int((reference * float(m.group(1))) // 100)
    return None


@dataclass
class OverlayOptions:
    width: object = None          # int or "NN%"
    min_width: int | None = None
    max_height: object = None     # int or "NN%"
    anchor: str = "center"
    offset_x: int = 0
    offset_y: int = 0
    row: object = None
    col: object = None
    margin: int = 0
    non_capturing: bool = False


def _resolve_anchor_row(anchor: str, height: int, avail_height: int, margin_top: int) -> int:
    if anchor in ("top-left", "top-center", "top-right"):
        return margin_top
    if anchor in ("bottom-left", "bottom-center", "bottom-right"):
        return margin_top + avail_height - height
    return margin_top + (avail_height - height) // 2  # left-center/center/right-center


def _resolve_anchor_col(anchor: str, width: int, avail_width: int, margin_left: int) -> int:
    if anchor in ("top-left", "left-center", "bottom-left"):
        return margin_left
    if anchor in ("top-right", "right-center", "bottom-right"):
        return margin_left + avail_width - width
    return margin_left + (avail_width - width) // 2  # top-center/center/bottom-center


class OverlayEntry:
    def __init__(self, component: Component, options: OverlayOptions, pre_focus, focus_order: int):
        self.component = component
        self.options = options
        self.pre_focus = pre_focus
        self.hidden = False
        self.focus_order = focus_order

    def layout(self, term_width: int, term_height: int, overlay_height: int = 0) -> tuple[int, int, int, int | None]:
        """Returns (width, row, col, max_height) for this overlay against
        the given terminal size. Width and max_height don't depend on
        overlay_height (only row/col placement does — a bottom/right
        anchored box needs to know its own height before it can know
        where its top edge lands), so composite_overlays calls this
        twice: once with overlay_height=0 just to learn width/max_height,
        renders the component at that width, then calls again with the
        real rendered height to get the final row/col."""
        return self._layout(overlay_height, term_width, term_height)

    def _layout(self, overlay_height: int, term_width: int, term_height: int):
        opt = self.options
        margin_top = margin_right = margin_bottom = margin_left = max(0, opt.margin)
        avail_width = max(1, term_width - margin_left - margin_right)
        avail_height = max(1, term_height - margin_top - margin_bottom)

        width = _parse_size_value(opt.width, term_width)
        if width is None:
            width = min(80, avail_width)
        if opt.min_width is not None:
            width = max(width, opt.min_width)
        width = max(1, min(width, avail_width))

        max_height = _parse_size_value(opt.max_height, term_height)
        if max_height is not None:
            max_height = max(1, min(max_height, avail_height))

        effective_height = min(overlay_height, max_height) if max_height is not None else overlay_height

        if opt.row is not None:
            if isinstance(opt.row, str):
                m = re.match(r"^(\d+(?:\.\d+)?)%$", opt.row)
                if m:
                    max_row = max(0, avail_height - effective_height)
                    row = margin_top + int(max_row * (float(m.group(1)) / 100))
                else:
                    row = _resolve_anchor_row("center", effective_height, avail_height, margin_top)
            else:
                row = opt.row
        else:
            row = _resolve_anchor_row(opt.anchor, effective_height, avail_height, margin_top)

        if opt.col is not None:
            if isinstance(opt.col, str):
                m = re.match(r"^(\d+(?:\.\d+)?)%$", opt.col)
                if m:
                    max_col = max(0, avail_width - width)
                    col = margin_left + int(max_col * (float(m.group(1)) / 100))
                else:
                    col = _resolve_anchor_col("center", width, avail_width, margin_left)
            else:
                col = opt.col
        else:
            col = _resolve_anchor_col(opt.anchor, width, avail_width, margin_left)

        row += opt.offset_y
        col += opt.offset_x
        row = max(margin_top, min(row, term_height - margin_bottom - effective_height))
        col = max(margin_left, min(col, term_width - margin_right - width))
        return width, row, col, max_height


class OverlayHandle:
    def __init__(self, screen: "TuiAltScreen", entry: OverlayEntry):
        self._screen = screen
        self._entry = entry

    def hide(self) -> None:
        if self._entry in self._screen.overlay_stack:
            self._screen.overlay_stack.remove(self._entry)

    def set_hidden(self, hidden: bool) -> None:
        self._entry.hidden = hidden

    def is_hidden(self) -> bool:
        return self._entry.hidden

    def focus(self) -> None:
        self._screen.focus_order_counter += 1
        self._entry.focus_order = self._screen.focus_order_counter

    def is_focused(self) -> bool:
        return self._screen.focused_overlay is self._entry


# --- Selection ---------------------------------------------------------
# Port of the character/word/line drag-select machinery in
# tui-alt-screen.ts: getSelectionBounds, getSelectionColumns,
# applySelectionHighlight, getWordSelection, getLineSelection, plus the
# button-press/drag/release state machine (handleSelectionMouseEvent).
#
# A press outside the primary ScrollView selects against the composited
# screen (self.previous_screen) in screen rows. A press inside it selects in
# CONTENT rows (scroll_top + row-in-viewport), so the range survives
# scrolling (wheel or drag auto-scroll) and copy reads the full content, not
# just the painted window.
_WORD_RE = re.compile(r"\w+|[^\w\s]+|\s+", re.UNICODE)


@dataclass
class SelectionPoint:
    row: int
    col: int
    boundary: bool = False


@dataclass
class ClickTarget:
    timestamp: float
    count: int
    row: int
    word_start: int
    word_end: int


DOUBLE_CLICK_INTERVAL_S = 0.5


def _word_segments(line: str) -> list[str]:
    return _WORD_RE.findall(line)


class TuiAltScreen:
    def __init__(self, root: Component):
        self.root = root
        self.previous_screen: list[str] = []
        self.previous_width = 0
        self.previous_height = 0
        # Kitty images are a compositing layer independent of the text
        # grid (see message_row_.Image) — a placement stays visually on
        # screen forever unless something explicitly deletes it, scrolling
        # it out of the diffed screen does NOT remove it. Track which image
        # ids are visible in this frame and evict anything no longer part of
        # the current frame. This avoids showing stale image placements from
        # previous renders.
        self._visible_kitty_image_ids: set[int] = set()

        self.flashes = AltScreenFlashContainer()

        self.overlay_stack: list[OverlayEntry] = []
        self.focus_order_counter = 0
        self.focused_overlay: OverlayEntry | None = None

        self.selection_anchor: SelectionPoint | None = None
        self.selection_focus: SelectionPoint | None = None
        self.selection_granularity = "character"  # "character" | "word" | "line"
        self.selection_initial_range: tuple[SelectionPoint, SelectionPoint] | None = None
        self.selection_press_active = False
        self._last_click: ClickTarget | None = None
        self.selection_in_content = False   # points are content rows of primary_scroll_view
        self.drag_edge = 0                  # -1/0/1: drag is parked at top/bottom edge of the viewport
        self._drag_x = 0
        self._last_autoscroll = 0.0
        self._content_cache: list[str] | None = None

        # The scroll view search highlighting/navigation operates on.
        # Whoever builds the actual widget tree hands this in explicitly
        # (set_primary_scroll_view) — this class has no way to guess which
        # of possibly several ScrollViews the user means by "search".
        self.primary_scroll_view = None
        self.search_query: str = ""
        self.search_matches: list[SearchMatch] = []
        self.search_selected: int = -1

    def _terminal_size(self) -> tuple[int, int]:
        size = shutil.get_terminal_size(fallback=(80, 24))
        return size.columns, size.lines

    # --- overlays ---------------------------------------------------
    def invalidate_overlays(self) -> None:
        """Drop every open overlay's cached render — what a live theme switch
        calls so pickers/panels repaint in the new color set instead of
        serving lines built with the old one."""
        for entry in self.overlay_stack:
            try:
                entry.component.invalidate()
            except Exception:
                pass

    def show_overlay(self, component: Component, options: OverlayOptions | None = None) -> OverlayHandle:
        self.focus_order_counter += 1
        entry = OverlayEntry(component, options or OverlayOptions(), self.focused_overlay, self.focus_order_counter)
        self.overlay_stack.append(entry)
        if not entry.options.non_capturing:
            self.focused_overlay = entry
        return OverlayHandle(self, entry)

    def hide_overlay(self) -> None:
        if self.overlay_stack:
            self.overlay_stack.pop()

    def has_overlay(self) -> bool:
        return any(not e.hidden for e in self.overlay_stack)

    # --- flash --------------------------------------------------------
    def flash(self, message: str, duration_ms: float = 1000) -> None:
        self.flashes.flash(message, duration_ms)

    # --- search ---------------------------------------------------------
    def set_primary_scroll_view(self, scroll_view) -> None:
        """Register which ScrollView "search" operates on. Must be the
        same object instance passed to the VStack that ends up as
        self.root (directly, or nested inside another VStack) — this is
        how apply_search_highlights finds it on screen again via
        root.find_offset(scroll_view)."""
        self.primary_scroll_view = scroll_view

    def set_search_query(self, query: str) -> None:
        if query == self.search_query:
            return
        self.search_query = query
        self.search_matches = []
        self.search_selected = -1

    def search_next(self) -> None:
        if self.search_matches:
            self.search_selected = (self.search_selected + 1) % len(self.search_matches)

    def search_previous(self) -> None:
        if self.search_matches:
            self.search_selected = (self.search_selected - 1) % len(self.search_matches)

    def _refresh_search_matches(self, width: int) -> None:
        """Re-scan the primary scroll view's full content for the current
        query. Runs once per frame (from do_render, only while a query is
        set) rather than once when the query changes, on purpose: content
        streaming in during an open search should turn up new matches
        without the user having to retype anything. Case-insensitive
        plain-text substring search — no fuzzy matching, no regex; add
        those later behind the same query string if wanted."""
        sv = self.primary_scroll_view
        query = self.search_query.strip()
        previous_selected = (
            self.search_matches[self.search_selected]
            if 0 <= self.search_selected < len(self.search_matches) else None
        )
        if not sv or not query:
            self.search_matches = []
            self.search_selected = -1
            return
        content_lines = sv.child.render(width)
        needle = query.lower()
        matches: list[SearchMatch] = []
        for row, line in enumerate(content_lines):
            plain = strip_terminal_sequences(line).lower()
            start = 0
            while True:
                idx = plain.find(needle, start)
                if idx == -1:
                    break
                matches.append(SearchMatch(row, idx, idx + len(needle)))
                start = idx + max(1, len(needle))
        self.search_matches = matches
        if previous_selected is not None:
            try:
                self.search_selected = matches.index(previous_selected)
                return
            except ValueError:
                pass
        self.search_selected = 0 if matches else -1

    def apply_search_highlights(self, screen: list[str]) -> list[str]:
        sv = self.primary_scroll_view
        if not sv or not self.search_matches:
            return screen
        offset = self.root.find_offset(sv) if hasattr(self.root, "find_offset") else None
        if offset is None:
            return screen
        top = sv.scroll_top
        viewport_height = sv.viewport_height
        result = list(screen)
        for i, m in enumerate(self.search_matches):
            screen_row = offset + (m.row - top)
            if m.row < top or m.row >= top + viewport_height:
                continue  # scrolled off-screen this frame
            if not (0 <= screen_row < len(result)):
                continue
            line = result[screen_row]
            line_width = visible_width(line)
            start_col = min(m.start_col, line_width)
            end_col = min(m.end_col, line_width)
            if end_col <= start_col:
                continue
            before = slice_by_column(line, 0, start_col, True)
            hit = slice_by_column(line, start_col, end_col - start_col, True)
            after = slice_by_column(line, end_col, max(0, line_width - end_col), True)
            current = i == self.search_selected
            style, reset = ("\x1b[1;7m", "\x1b[22;27m") if current else ("\x1b[4m", "\x1b[24m")
            result[screen_row] = f"{before}{style}{hit}{reset}{after}"
        return result

    # --- selection: text lookups over the last composited screen ------
    def _scroll_region(self):
        """(scroll_view, screen_offset, viewport_height) of the primary scroll view, or None."""
        sv = self.primary_scroll_view
        if sv is None or not hasattr(self.root, "find_offset"):
            return None
        offset = self.root.find_offset(sv)
        if offset is None or sv.viewport_height <= 0:
            return None
        return sv, offset, sv.viewport_height

    def _content_lines(self) -> list[str]:
        if self._content_cache is None:
            self._content_cache = [
                strip_osc133_zone(l) for l in self.primary_scroll_view.child.render(self._terminal_size()[0])
            ]
        return self._content_cache

    def _selection_source_line(self, row: int) -> str:
        lines = self._content_lines() if self.selection_in_content else self.previous_screen
        return lines[row] if 0 <= row < len(lines) else ""

    def autoscroll_tick(self) -> bool:
        """Called every frame by the app's render loop: while a drag is parked
        on the viewport's top/bottom edge, scroll one line and extend the
        selection to the newly revealed edge row (motion events stop when the
        mouse sits still, so this can't be event-driven). True if it scrolled."""
        if not (self.selection_press_active and self.selection_in_content and self.drag_edge):
            return False
        now = _time.monotonic()
        if now - self._last_autoscroll < 0.04:
            return False
        region = self._scroll_region()
        if region is None:
            return False
        sv, offset, height = region
        if sv.scroll_by(self.drag_edge) == self.drag_edge:
            return False  # already at the end of the content
        self._last_autoscroll = now
        edge_row = offset if self.drag_edge < 0 else offset + height - 1
        self._update_selection_focus(SelectionPoint(edge_row - offset + sv.scroll_top, self._drag_x))
        return True

    def _word_selection(self, point: SelectionPoint) -> tuple[SelectionPoint, SelectionPoint] | None:
        line = strip_terminal_sequences(self._selection_source_line(point.row))
        start = 0
        for segment in _word_segments(line):
            end = start + len(segment)
            if start <= point.col < end:
                return SelectionPoint(point.row, start), SelectionPoint(point.row, end, boundary=True)
            start = end
        return None

    def _line_selection(self, point: SelectionPoint) -> tuple[SelectionPoint, SelectionPoint]:
        width = visible_width(self._selection_source_line(point.row))
        return SelectionPoint(point.row, 0), SelectionPoint(point.row, width, boundary=True)

    def _update_selection_focus(self, point: SelectionPoint) -> None:
        if self.selection_granularity == "character" or not self.selection_initial_range:
            self.selection_focus = point
            return
        rng = self._word_selection(point) if self.selection_granularity == "word" else self._line_selection(point)
        if not rng:
            return
        initial_start, initial_end = self.selection_initial_range
        range_start, range_end = rng
        target_before_initial = (range_start.row, range_start.col) < (initial_start.row, initial_start.col)
        if target_before_initial:
            self.selection_anchor, self.selection_focus = initial_end, range_start
        else:
            self.selection_anchor, self.selection_focus = initial_start, range_end

    def _get_click_count(self, point: SelectionPoint, word) -> int:
        now = _time.monotonic()
        prev = self._last_click
        count = 1
        if (
            word and prev
            and now - prev.timestamp <= DOUBLE_CLICK_INTERVAL_S
            and prev.row == point.row
            and prev.word_start == word[0].col
            and prev.word_end == word[1].col
        ):
            count = (prev.count % 3) + 1
        self._last_click = ClickTarget(now, count, point.row, word[0].col, word[1].col) if word else None
        return count

    def get_selection_bounds(self) -> tuple[SelectionPoint, SelectionPoint] | None:
        a, f = self.selection_anchor, self.selection_focus
        if a is None or f is None:
            return None
        if (a.row, a.col) == (f.row, f.col):
            return None
        return (a, f) if (a.row, a.col) < (f.row, f.col) else (f, a)

    def _selection_columns(self, line: str, row: int, sel: tuple[SelectionPoint, SelectionPoint],
                            min_col: int = 0, max_col: int | None = None) -> tuple[int, int]:
        line_width = visible_width(line)
        if max_col is None:
            max_col = line_width
        start, end = sel
        col_start = max(0, min_col)
        col_end = min(line_width, max_col)
        if row == start.row:
            col_start = min(start.col, line_width)
        if row == end.row:
            col_end = min(end.col, line_width) if end.boundary else min(end.col + 1, line_width)
        return max(min_col, col_start), min(max_col, col_end)

    def _selection_highlight(self, text: str) -> str:
        result = "\x1b[7m"
        i = 0
        while i < len(text):
            ansi = extract_ansi_code(text, i)
            if not ansi:
                result += text[i]
                i += 1
                continue
            code, length = ansi
            result += code
            if code.endswith("m"):
                result += "\x1b[7m"
            i += length
        return result + "\x1b[27m"

    def apply_selection(self, screen: list[str]) -> list[str]:
        sel = self.get_selection_bounds()
        if not sel:
            return screen
        start, end = sel
        region = self._scroll_region() if self.selection_in_content else None
        if self.selection_in_content and region is None:
            return screen

        def transform(screen_row: int, line: str) -> str:
            row = screen_row
            if region:
                sv, offset, height = region
                if not offset <= screen_row < offset + height:
                    return line
                row = screen_row - offset + sv.scroll_top
            if row < start.row or row > end.row:
                return line
            col_start, col_end = self._selection_columns(line, row, sel)
            if col_end <= col_start:
                return line
            line_width = visible_width(line)
            before = slice_by_column(line, 0, col_start, True)
            selected = slice_by_column(line, col_start, col_end - col_start, True)
            after = slice_by_column(line, col_end, max(0, line_width - col_end), True)
            return f"{before}{self._selection_highlight(selected)}{after}"

        return [transform(row, line) for row, line in enumerate(screen)]

    def copy_selection_to_clipboard(self) -> str | None:
        """Writes the selected plain text to the system clipboard (None if
        nothing is selected). pyperclip first — it shells out to pbcopy on
        macOS, which actually lands in the real clipboard regardless of
        any terminal setting — OSC 52 only as a fallback for pyperclip-less
        environments (a real remote terminal over SSH). OSC 52 alone used
        to be the only path here, which is why the "Copied!" flash used to
        fire unconditionally: Terminal.app doesn't implement OSC 52 at all,
        and iTerm2 ships it disabled by default (Preferences > General >
        Selection > "Applications in terminal may access clipboard"), so
        on both the escape sequence was accepted and silently dropped —
        nothing to detect failure from, hence the always-on flash. Mirrors
        MicroTui.copy_to_clipboard's same pyperclip-then-OSC52 order in
        start_live_tui_.py, which never had this bug because it checks
        whether pyperclip actually landed before claiming success."""
        sel = self.get_selection_bounds()
        if not sel:
            return None
        start, end = sel
        lines = []
        for row in range(start.row, end.row + 1):
            line = self._selection_source_line(row)
            col_start, col_end = self._selection_columns(line, row, sel)
            fragment = slice_by_column(line, col_start, max(0, col_end - col_start), True)
            lines.append(strip_terminal_sequences(fragment).rstrip())
        text = "\n".join(lines)
        if not text:
            return None

        landed = False
        pyperclip_error = None
        try:
            import pyperclip
            pyperclip.copy(text)
            landed = True
        except Exception as e:
            # No disk log to stash this in (shipped product — see
            # start_live_tui_.py's background-noise handling), so the
            # only way to ever find out *why* pyperclip failed on a given
            # machine is to put the real exception in front of the user
            # right here instead of silently falling back to OSC 52.
            pyperclip_error = f"{type(e).__name__}: {e}"
        if not landed:
            try:
                import base64
                sys.stdout.write(f"\x1b]52;c;{base64.b64encode(text.encode()).decode()}\x07")
                sys.stdout.flush()
            except OSError:
                pass
            self.flash(f"pyperclip failed ({pyperclip_error}) — used terminal clipboard instead, enable clipboard access if it didn't land")
        else:
            self.flash("Copied!")
        return text

    # --- mouse: SGR (1006) press/drag/release -> selection state ------
    _SGR_RE = re.compile(r"^\x1b\[<(\d+);(\d+);(\d+)([Mm])$")

    @classmethod
    def parse_sgr_mouse_event(cls, data: str):
        m = cls._SGR_RE.match(data)
        if not m:
            return None
        button, x, y, kind = int(m.group(1)), int(m.group(2)) - 1, int(m.group(3)) - 1, m.group(4)
        return {"button": button, "x": x, "y": y, "release": kind == "m"}

    def _content_point(self, region, point: SelectionPoint) -> SelectionPoint:
        """Screen point -> content-row point, clamped into the viewport."""
        sv, offset, height = region
        screen_row = max(offset, min(offset + height - 1, point.row))
        return SelectionPoint(screen_row - offset + sv.scroll_top, point.col)

    def handle_selection_mouse_event(self, event: dict) -> None:
        term_width, term_height = self._terminal_size()
        button = event["button"] & 3
        if button != 0 and not (event["release"] and button == 3):
            return
        point = SelectionPoint(
            row=max(0, min(term_height - 1, event["y"])),
            col=max(0, min(term_width - 1, event["x"])),
        )
        region = self._scroll_region()
        if self.selection_press_active and self.selection_in_content and region:
            point = self._content_point(region, point)
        if event["release"]:
            if not self.selection_press_active:
                return
            self.selection_press_active = False
            self.drag_edge = 0
            if self.selection_anchor is None:
                return
            self._update_selection_focus(point)
            self.copy_selection_to_clipboard()
            return
        if event["button"] & 32:  # drag (motion while a button is held)
            if not self.selection_press_active or self.selection_anchor is None:
                return
            self._last_click = None
            if self.selection_in_content and region:
                sv, offset, height = region
                self._drag_x = point.col
                self.drag_edge = -1 if event["y"] <= offset else (1 if event["y"] >= offset + height - 1 else 0)
            self._update_selection_focus(point)
            return
        # fresh press
        self.selection_press_active = True
        self.drag_edge = 0
        self.selection_in_content = bool(
            region and region[1] <= point.row < region[1] + region[2] and not self.has_overlay()
        )
        if self.selection_in_content:
            point = self._content_point(region, point)
        word = self._word_selection(point)
        click_count = self._get_click_count(point, word)
        rng = word if click_count == 2 else (self._line_selection(point) if click_count == 3 else None)
        self.selection_granularity = "word" if click_count == 2 else ("line" if click_count == 3 else "character")
        self.selection_initial_range = rng
        self.selection_anchor = rng[0] if rng else point
        self.selection_focus = rng[1] if rng else point

    def extract_cursor_position(self, screen: list[str], height: int):
        """Find CURSOR_MARKER in the bottom `height` rows, strip it from
        `screen` in place, and return (row, col). None if not found."""
        viewport_top = max(0, len(screen) - height)
        for row in range(len(screen) - 1, viewport_top - 1, -1):
            idx = screen[row].find(CURSOR_MARKER)
            if idx != -1:
                col = visible_width(screen[row][:idx])
                screen[row] = screen[row][:idx] + screen[row][idx + len(CURSOR_MARKER):]
                return row, col
        return None

    def do_render(self) -> None:
        width, height = self._terminal_size()
        width, height = max(1, width), max(1, height)
        self._content_cache = None

        layout_lines = render_layout_frame(self.root, width, height)
        screen = [strip_osc133_zone(line) for line in layout_lines]
        if self.search_query:
            self._refresh_search_matches(width)
            screen = self.apply_search_highlights(screen)
        screen = composite_overlays(screen, width, height, self.overlay_stack)
        # Everything downstream (apply_selection, composite_flashes,
        # extract_cursor_position, and the row-write loop below) assumes
        # screen has EXACTLY `height` rows. render_layout_frame is supposed
        # to guarantee that already, but a layout-math edge case (e.g. a
        # VStack's grow/shrink distribution not fully resolving) can leave
        # it a row short — and only truncating the too-long case here left
        # that short case to blow up as an uncaught IndexError several
        # layers down instead of being silently corrected here, where the
        # actual invariant is meant to hold. See last_render_crash.log's
        # IndexError at the row-write loop for the bug this fixes.
        if len(screen) > height:
            screen = screen[len(screen) - height:]
        elif len(screen) < height:
            screen = screen + [""] * (height - len(screen))
        screen = self.apply_selection(screen)
        screen = composite_flashes(screen, width, height, self.flashes)
        cursor_pos = self.extract_cursor_position(screen, height)
        screen = [clip_overwide_line(apply_line_resets(line), width) for line in screen]

        # Anything tracked as visible last frame that isn't in this frame's
        # visible set has scrolled out of the viewport (or the row it was
        # on now holds something else) — Kitty won't drop it on its own,
        # so tell it to explicitly. See extract_kitty_image_ids's docstring
        # and TuiAltScreen.__init__'s _visible_kitty_image_ids comment.
        current_kitty_image_ids = extract_kitty_image_ids(screen)
        evicted_kitty_image_ids = self._visible_kitty_image_ids - current_kitty_image_ids
        self._visible_kitty_image_ids = current_kitty_image_ids

        full_redraw = (
            not self.previous_screen
            or self.previous_width != width
            or self.previous_height != height
        )

        buf = BEGIN_SYNCHRONIZED_OUTPUT
        for evicted_id in evicted_kitty_image_ids:
            buf += f"\x1b_Ga=d,d=I,i={evicted_id},q=2\x1b\\"
        if full_redraw:
            buf += "\x1b[2J"
        for row in range(height):
            changed = (
                full_redraw
                or row >= len(self.previous_screen)
                or screen[row] != self.previous_screen[row]
            )
            if changed:
                buf += f"\x1b[{row + 1};1H\x1b[2K{screen[row]}"
        if cursor_pos:
            buf += f"\x1b[{cursor_pos[0] + 1};{min(width, cursor_pos[1]) + 1}H\x1b[?25h"
        else:
            buf += "\x1b[?25l"
        buf += END_SYNCHRONIZED_OUTPUT

        sys.stdout.write(buf)
        sys.stdout.flush()

        self.previous_screen, self.previous_width, self.previous_height = screen, width, height

