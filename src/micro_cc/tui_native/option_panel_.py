"""Option Panel with label/description items."""

from dataclasses import dataclass, field

from micro_cc.tui_native.text_utils_ import slice_by_column, visible_width
from micro_cc.tui_native.theme_ import default_theme, Theme

DEFAULT_MAX_VISIBLE = 8
PRIMARY_COLUMN_GAP = 2
MIN_DESCRIPTION_WIDTH = 10
DEFAULT_PRIMARY_MIN = 12
DEFAULT_PRIMARY_MAX = 32

MAX_DESC_LINES = 3


@dataclass
class PanelItem:
    value: str
    label: str
    description: str = ""
    choices: list = field(default_factory=list)  # ←/→ cycles; empty = no toggle
    choice: int = 0

    @property
    def chosen(self):
        return self.choices[self.choice] if self.choices else None


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


class OptionPanel:
    """Scrolling option list with optional ←/→ choice per item and multi-select mode."""

    def __init__(self,
        show,
        hide,
        max_visible: int = DEFAULT_MAX_VISIBLE,
        primary_min: int = DEFAULT_PRIMARY_MIN,
        primary_max: int = DEFAULT_PRIMARY_MAX,
        theme: Theme | None = None,
        max_desc_lines: int = MAX_DESC_LINES,
    ):
        self._show, self._hide = show, hide  # injected by the host: where the panel lives
        self.is_open = False
        self.multi = False
        self.title = ""
        self.checked: set[str] = set()
        self.on_select = None            # single: (PanelItem); multi: (list[PanelItem])
        self.on_cancel = None            # ()
        self.on_selection_change = None  # (PanelItem)
        self.max_visible = max(1, max_visible)
        self.primary_min = max(1, min(primary_min, primary_max))
        self.primary_max = max(1, max(primary_min, primary_max))
        self.theme = theme  # None = the active color set, read at render
        self.max_desc_lines = max(1, max_desc_lines)
        self._all_items: list[PanelItem] = []
        self.items: list[PanelItem] = []
        self.selected_index = 0
        self._hit_rows: list[tuple[int, int, int]] = []  # (first_row, last_row, item index)

    # --- lifecycle -----------------------------------------------------
    def open(self, items, on_select, on_cancel=None, title="", multi=False, selected=None, checked=None) -> None:
        self.set_items(items)
        self.on_select, self.on_cancel = on_select, on_cancel
        self.title, self.multi, self.checked = title, multi, set(checked or ())
        if selected is not None:
            self.set_selected_value(selected)
        self.is_open = True
        self._show(self)

    def close(self) -> None:
        self.is_open = False
        self._hide()

    def confirm(self) -> None:
        if self.multi:
            result = [it for it in self._all_items if it.value in self.checked]
        else:
            result = self.get_selected_item()
            if result is None:
                return
        on_select = self.on_select
        self.close()  # close first so the callback may reopen the panel
        if on_select is not None:
            on_select(result)

    def cancel(self) -> None:
        on_cancel = self.on_cancel
        self.close()
        if on_cancel is not None:
            on_cancel()

    # --- content -----------------------------------------------------
    def set_items(self, items) -> None:
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

    def get_selected_item(self) -> PanelItem | None:
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

    def cycle_choice(self, step: int) -> bool:
        item = self.get_selected_item()
        if item is None or not item.choices:
            return False
        item.choice = (item.choice + step) % len(item.choices)
        return True

    def toggle(self) -> None:
        item = self.get_selected_item()
        if item is not None:
            self.checked ^= {item.value}

    def click_row(self, row: int) -> bool:
        """Select (and confirm, or toggle in multi) the item at row."""
        for first, last, idx in self._hit_rows:
            if first <= row <= last:
                self.selected_index = idx
                self._notify_change()
                self.toggle() if self.multi else self.confirm()
                return True
        return False

    def handle_key(self, key: str) -> bool:
        """Handle up/down/left/right/space/enter/escape; return True if consumed."""
        if key == "up":
            self.move_up()
        elif key == "down":
            self.move_down()
        elif key in ("left", "right"):
            self.cycle_choice(-1 if key == "left" else 1)  # swallowed even when inert: no stray cursor keys
        elif key in (" ", "space") and self.multi:
            self.toggle()
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
        if not self.is_open:
            return []
        theme = self.theme or default_theme()
        lines = [theme.title(f" {w}") for w in _wrap_text(self.title, max(1, width - 2))] if self.title else []
        if not self.items:
            return lines + [theme.no_match("  No matches")]

        self._hit_rows = []
        primary_width = self._primary_column_width()
        start = max(0, min(self.selected_index - self.max_visible // 2, len(self.items) - self.max_visible))
        end = min(start + self.max_visible, len(self.items))
        for i in range(start, end):
            item_lines = self._render_item(self.items[i], i == self.selected_index, width, primary_width, theme)
            self._hit_rows.append((len(lines), len(lines) + len(item_lines) - 1, i))
            lines.extend(item_lines)
        hints = ["↑↓ move", "⏎ select", "esc cancel"]
        if self.multi:
            hints.insert(1, "space toggle")
        if any(it.choices for it in self.items):
            hints.insert(1, "←→ change")
        if start > 0 or end < len(self.items):
            hints.append(f"{self.selected_index + 1}/{len(self.items)}")
        lines.append(theme.scroll_info(_truncate("  " + " · ".join(hints), width)))
        return lines

    def _primary_column_width(self) -> int:
        widest = max((visible_width(it.label) + PRIMARY_COLUMN_GAP for it in self.items), default=0)
        return max(self.primary_min, min(widest, self.primary_max))

    def _render_item(self, item: PanelItem, is_selected: bool, width: int, primary_width: int, theme : Theme) -> list[str]:
        """Render item as 1+ lines; try two-column layout, fall back to wrapped description."""
        prefix = "\u258c " if is_selected else "  "
        if self.multi:
            box = "[x] " if item.value in self.checked else "[ ] "
            prefix += box if is_selected else theme.checked(box)  # selected row is one style span
        prefix_w = visible_width(prefix)
        indent = " " * prefix_w
        desc = " ".join(item.description.split()) if item.description else ""
        if item.choices:
            chosen = f"\u2039 {item.chosen} \u203a" if is_selected else f"  {item.chosen}  "  # same width: no jitter
            pad = " " * max(PRIMARY_COLUMN_GAP, primary_width - visible_width(item.label))
            item = PanelItem(item.value, f"{item.label}{pad}{chosen}", item.description)
        style = theme.selected_text if is_selected else (lambda s: s)

        if desc:
            if width > 40:
                col_w = max(1, min(primary_width, width - prefix_w - 4))
                spacing = " " * PRIMARY_COLUMN_GAP
                desc_w = width - prefix_w - col_w - len(spacing)
                if visible_width(item.label) <= col_w and visible_width(desc) <= desc_w:
                    pad = " " * max(1, col_w - visible_width(item.label))
                    if is_selected:
                        return [style(f"{prefix}{item.label}{pad}{desc}")]
                    return [f"{prefix}{item.label}{pad}{theme.description(desc)}"]
            # Narrow width or two-column didn't fit: show label then wrapped description below.
            lines = [style(f"{prefix}{item.label}")]
            for wline in _wrap_text_capped(desc, max(1, width - prefix_w), self.max_desc_lines):
                lines.append(style(f"{indent}{wline}") if is_selected else f"{indent}{theme.description(wline)}")
            return lines

        wrapped = _wrap_text(item.label, max(1, width - prefix_w))
        return [style(f"{prefix}{wrapped[0]}")] + [style(f"{indent}{w}") for w in wrapped[1:]]
