"""Single-select list picker. Layer 1 only (see chat): a dumb, reusable
component with plain callback attributes, no async/Future inside it —
whatever opens one wires on_select/on_cancel to its own cleanup and
resolves its own asyncio.Future/whatever at the call site. Covers every
current single-choice picker (slash, at, model, rewind, login) and the
single-choice branch of an ask_user_question stage — all of them are
"list of {label, description}, pick one".

Item shape mirrors tools/ask_user_tool.py's AskOption exactly
(label + description, both plain strings) so a question's options can be
handed to this directly with no reshaping — value defaults to label
when nothing else makes sense as a stable id (that's also exactly what
the current Textual pickers do: Option(label, id=label)).
"""

from dataclasses import dataclass, field

from micro_cc.tui_native.text_utils_ import slice_by_column, visible_width
from micro_cc.tui_native.theme_ import default_theme, Theme

DEFAULT_MAX_VISIBLE = 8
PRIMARY_COLUMN_GAP = 2
MIN_DESCRIPTION_WIDTH = 10
DEFAULT_PRIMARY_MIN = 12
DEFAULT_PRIMARY_MAX = 32
# One long description must not be able to blow the whole panel past the
# terminal height on its own (see start_live_tui_'s ask-question overflow —
# alt_screen_.render's "keep last `height` rows" fallback then eats the
# question text and the panel's own top rows). Capping per-item growth here
# is what makes a bounded max_visible in the caller actually bound the total.
MAX_DESC_LINES = 3


@dataclass
class PickerItem:
    value: str
    label: str
    description: str = ""


def _truncate(text: str, max_width: int, ellipsis: str = "\u2026") -> str:
    if max_width <= 0:
        return ""
    if visible_width(text) <= max_width:
        return text
    ell_w = visible_width(ellipsis)
    if ell_w >= max_width:
        return slice_by_column(ellipsis, 0, max_width, True)
    return slice_by_column(text, 0, max_width - ell_w, True) + ellipsis


def _wrap_text_capped(text: str, width: int, max_lines: int) -> list[str]:
    """_wrap_text, but hard-capped at `max_lines` — the last shown line gets
    ellipsized if that drops real content, so a single pathologically long
    description reads as truncated rather than silently eating the rest of
    the panel's height."""
    lines = _wrap_text(text, width)
    if len(lines) <= max_lines:
        return lines
    kept = lines[:max_lines]
    ellipsis = "…"
    ell_w = visible_width(ellipsis)
    room = max(0, width - ell_w)
    kept[-1] = slice_by_column(kept[-1], 0, room, True) + ellipsis
    return kept


def _wrap_text(text: str, width: int) -> list[str]:
    """Greedy word-wrap to `width` columns \u2014 a single word wider than
    `width` on its own (a long path, a URL) gets hard-broken at the
    column boundary instead of overflowing."""
    if width <= 0 or not text:
        return [""]
    lines: list[str] = []
    current = ""
    for word in text.split():
        candidate = f"{current} {word}" if current else word
        if visible_width(candidate) <= width:
            current = candidate
            continue
        if current:
            lines.append(current)
            current = ""
        while visible_width(word) > width:
            lines.append(slice_by_column(word, 0, width, True))
            word = word[len(slice_by_column(word, 0, width, True)):]
        current = word
    if current:
        lines.append(current)
    return lines or [""]


def wrap_in_border(lines: list[str], width: int) -> list[str]:
    """Minimal single-line border shared by every picker overlay \u2014 thin
    rounded corners, one space of padding, nothing ornamental."""
    inner_width = max(1, width - 2)
    top = "\u256d" + "\u2500" * inner_width + "\u256e"
    bottom = "\u2570" + "\u2500" * inner_width + "\u256f"
    body = []
    for line in lines:
        w = visible_width(line)
        pad = max(0, inner_width - w)
        body.append("\u2502" + line + " " * pad + "\u2502")
    return [top] + body + [bottom]


class ListPicker:
    """Component protocol (render/invalidate) plus handle_key. Up/down
    clamp at the ends (no wraparound — that jumped straight from the
    first item to the last, or vice versa, and read as a bug rather than
    a feature). Shows a scrolling window of max_visible items centered on
    the selection, with a "(n/total)" indicator once there are more items
    than fit."""

    def __init__(self, items: list[PickerItem] | None = None, max_visible: int = DEFAULT_MAX_VISIBLE,
                 primary_min: int = DEFAULT_PRIMARY_MIN, primary_max: int = DEFAULT_PRIMARY_MAX,
                 theme: Theme | None = None, max_desc_lines: int = MAX_DESC_LINES):
        self.on_select = None            # Callable[[PickerItem], None]
        self.on_cancel = None            # Callable[[], None]
        self.on_selection_change = None  # Callable[[PickerItem], None]
        self.max_visible = max(1, max_visible)
        self.primary_min = max(1, min(primary_min, primary_max))
        self.primary_max = max(1, max(primary_min, primary_max))
        self.theme = theme or default_theme()
        # Per-item description wrap cap. Was the module constant MAX_DESC_LINES
        # baked in directly, which ellipsized any hint past 3 lines even when
        # the terminal had plenty of room — callers with a real height
        # budget (e.g. the ask-question panel) can now size this to the
        # actual available space instead.
        self.max_desc_lines = max(1, max_desc_lines)
        self._all_items: list[PickerItem] = list(items or [])
        self.items: list[PickerItem] = list(self._all_items)
        self.selected_index = 0

    # --- content -----------------------------------------------------
    def set_items(self, items: list[PickerItem]) -> None:
        self._all_items = list(items)
        self.items = list(self._all_items)
        self.selected_index = 0

    def set_filter(self, text: str) -> None:
        """Prefix match on label, case-insensitive — same rule the real
        _update_slash_picker uses today. Resets selection to the top."""
        needle = text.lower()
        self.items = [it for it in self._all_items if it.label.lower().startswith(needle)] if needle else list(self._all_items)
        self.selected_index = 0

    def set_selected_value(self, value) -> None:
        for i, it in enumerate(self.items):
            if it.value == value:
                self.selected_index = i
                return

    def get_selected_item(self) -> PickerItem | None:
        if 0 <= self.selected_index < len(self.items):
            return self.items[self.selected_index]
        return None

    # --- navigation ----------------------------------------------------
    def _notify_change(self) -> None:
        item = self.get_selected_item()
        if item is not None and self.on_selection_change is not None:
            self.on_selection_change(item)

    def move_up(self) -> None:
        if not self.items:
            return
        self.selected_index = max(0, self.selected_index - 1)
        self._notify_change()

    def move_down(self) -> None:
        if not self.items:
            return
        self.selected_index = min(len(self.items) - 1, self.selected_index + 1)
        self._notify_change()

    def confirm(self) -> None:
        item = self.get_selected_item()
        if item is not None and self.on_select is not None:
            self.on_select(item)

    def cancel(self) -> None:
        if self.on_cancel is not None:
            self.on_cancel()

    def handle_key(self, key: str) -> bool:
        """key is a normalized name: 'up' / 'down' / 'enter' / 'escape'.
        Returns True if this picker consumed it. Anything else (typed
        characters) is the caller's own filter-text-box's business, not
        this component's — the picker and search box are separate concerns."""
        if key == "up":
            self.move_up()
        elif key == "down":
            self.move_down()
        elif key == "enter":
            self.confirm()
        elif key == "escape":
            self.cancel()
        else:
            return False
        return True

    # --- Component protocol --------------------------------------------
    def invalidate(self) -> None:
        pass

    def render(self, width: int) -> list[str]:
        inner_width = max(1, width - 2)  # 2 border columns, see wrap_in_border
        if not self.items:
            return wrap_in_border([self.theme.no_match("  No matches")], width)

        lines: list[str] = []
        primary_width = self._primary_column_width()
        start = max(0, min(self.selected_index - self.max_visible // 2, len(self.items) - self.max_visible))
        end = min(start + self.max_visible, len(self.items))
        for i in range(start, end):
            lines.extend(self._render_item(self.items[i], i == self.selected_index, inner_width, primary_width))
        if start > 0 or end < len(self.items):
            lines.append(self.theme.scroll_info(_truncate(f"  ({self.selected_index + 1}/{len(self.items)})", max(1, inner_width - 2))))
        return wrap_in_border(lines, width)

    def _primary_column_width(self) -> int:
        widest = max((visible_width(it.label) + PRIMARY_COLUMN_GAP for it in self.items), default=0)
        return max(self.primary_min, min(widest, self.primary_max))

    def _render_item(self, item: PickerItem, is_selected: bool, width: int, primary_width: int) -> list[str]:
        """One item -> one or more rendered lines. Label/description used
        to always get truncated with an ellipsis to force a single line \u2014
        now that pickers are as wide as their container (see
        MicroTui._open_picker), that truncation was throwing away real
        content on every long option for no reason. Still tries the
        single-line two-column layout first (the common case for short
        items); only wraps onto extra lines when label+description
        genuinely don't both fit at the available width."""
        prefix = "\u2192 " if is_selected else "  "
        prefix_w = visible_width(prefix)
        indent = " " * prefix_w
        desc = " ".join(item.description.split()) if item.description else ""
        style = self.theme.selected_text if is_selected else (lambda s: s)

        if desc:
            if width > 40:
                col_w = max(1, min(primary_width, width - prefix_w - 4))
                spacing = " " * PRIMARY_COLUMN_GAP
                desc_w = width - prefix_w - col_w - len(spacing)
                if visible_width(item.label) <= col_w and visible_width(desc) <= desc_w:
                    pad = " " * max(1, col_w - visible_width(item.label))
                    if is_selected:
                        return [style(f"{prefix}{item.label}{pad}{desc}")]
                    return [f"{prefix}{item.label}{pad}{self.theme.description(desc)}"]
            # Narrow width, or the two-column single-line layout didn't fit
            # both label and description: fall back to label then wrapped
            # description below it — never just drop the description, a
            # narrow terminal is exactly when the user needs it spelled out.
            lines = [style(f"{prefix}{item.label}")]
            for wline in _wrap_text_capped(desc, max(1, width - prefix_w), self.max_desc_lines):
                lines.append(style(f"{indent}{wline}") if is_selected else f"{indent}{self.theme.description(wline)}")
            return lines

        wrapped = _wrap_text(item.label, max(1, width - prefix_w))
        return [style(f"{prefix}{wrapped[0]}")] + [style(f"{indent}{w}") for w in wrapped[1:]]
