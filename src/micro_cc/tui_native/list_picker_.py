"""Single-select list picker with label/description items."""

from dataclasses import dataclass, field

from micro_cc.tui_native.text_utils_ import slice_by_column, visible_width
from micro_cc.tui_native.theme_ import default_theme, Theme

DEFAULT_MAX_VISIBLE = 8
PRIMARY_COLUMN_GAP = 2
MIN_DESCRIPTION_WIDTH = 10
DEFAULT_PRIMARY_MIN = 12
DEFAULT_PRIMARY_MAX = 32
# Cap per-item lines to bound total panel height; prevents long descriptions from blowing past terminal.
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
    """Wrap text capped at max_lines with ellipsis to indicate truncation."""
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
    """Greedy word-wrap with hard-break for words wider than width."""
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
    """Add minimal single-line border with rounded corners and padding."""
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
    """Scrolling single-select picker with up/down clamping and centered window."""

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
        # Per-item description wrap cap; allows callers to size based on available space.
        self.max_desc_lines = max(1, max_desc_lines)
        self._all_items: list[PickerItem] = list(items or [])
        self.items: list[PickerItem] = list(self._all_items)
        self.selected_index = 0
        self._hit_rows: list[tuple[int, int, int]] = []  # (first_row, last_row, item index) inside the border

    # --- content -----------------------------------------------------
    def set_items(self, items: list[PickerItem]) -> None:
        self._all_items = list(items)
        self.items = list(self._all_items)
        self.selected_index = 0

    def set_filter(self, text: str) -> None:
        """Prefix match on label (case-insensitive); reset selection to top."""
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

    def click_row(self, row: int) -> bool:
        """Select and confirm item at row; 0 is top border."""
        for first, last, idx in self._hit_rows:
            if first <= row <= last:
                self.selected_index = idx
                self._notify_change()
                self.confirm()
                return True
        return False

    def handle_key(self, key: str) -> bool:
        """Handle up/down/enter/escape; return True if consumed."""
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
        self._hit_rows = []
        primary_width = self._primary_column_width()
        start = max(0, min(self.selected_index - self.max_visible // 2, len(self.items) - self.max_visible))
        end = min(start + self.max_visible, len(self.items))
        for i in range(start, end):
            item_lines = self._render_item(self.items[i], i == self.selected_index, inner_width, primary_width)
            self._hit_rows.append((len(lines) + 1, len(lines) + len(item_lines), i))
            lines.extend(item_lines)
        if start > 0 or end < len(self.items):
            lines.append(self.theme.scroll_info(_truncate(f"  ({self.selected_index + 1}/{len(self.items)})", max(1, inner_width - 2))))
        return wrap_in_border(lines, width)

    def _primary_column_width(self) -> int:
        widest = max((visible_width(it.label) + PRIMARY_COLUMN_GAP for it in self.items), default=0)
        return max(self.primary_min, min(widest, self.primary_max))

    def _render_item(self, item: PickerItem, is_selected: bool, width: int, primary_width: int) -> list[str]:
        """Render item as 1+ lines; try two-column layout, fall back to wrapped description."""
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
            # Narrow width or two-column didn't fit: show label then wrapped description below.
            lines = [style(f"{prefix}{item.label}")]
            for wline in _wrap_text_capped(desc, max(1, width - prefix_w), self.max_desc_lines):
                lines.append(style(f"{indent}{wline}") if is_selected else f"{indent}{self.theme.description(wline)}")
            return lines

        wrapped = _wrap_text(item.label, max(1, width - prefix_w))
        return [style(f"{prefix}{wrapped[0]}")] + [style(f"{indent}{w}") for w in wrapped[1:]]
