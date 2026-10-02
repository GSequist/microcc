"""Multi-select picker matching AskUserSelect's shape: space toggles the
item under the cursor, enter confirms the checked set (in item order),
escape cancels. Cursor position and checked-state are independent —
you can move over an item without checking it. Reuses ListPicker's
rendering/navigation/filtering by composition rather than duplicating it."""

from dataclasses import dataclass

from micro_cc.tui_native.list_picker_ import MAX_DESC_LINES, ListPicker, PickerItem, wrap_in_border
from micro_cc.tui_native.theme_ import default_theme, Theme


class MultiSelectList:
    def __init__(self, items: list[PickerItem] | None = None, max_visible: int = 8,
                 primary_min: int = 12, primary_max: int = 32, theme: Theme | None = None,
                 max_desc_lines: int = MAX_DESC_LINES):
        self._picker = ListPicker(items, max_visible, primary_min, primary_max, theme, max_desc_lines)
        self.theme = theme or default_theme()
        self.checked: set[str] = set()

        self.on_confirm = None           # Callable[[list[PickerItem]], None]
        self.on_cancel = None            # Callable[[], None]
        self.on_selection_change = None  # Callable[[PickerItem], None]

        self._picker.on_selection_change = lambda item: (
            self.on_selection_change(item) if self.on_selection_change else None
        )

    @property
    def max_visible(self) -> int:
        return self._picker.max_visible

    @max_visible.setter
    def max_visible(self, value: int) -> None:
        self._picker.max_visible = max(1, value)

    @property
    def max_desc_lines(self) -> int:
        return self._picker.max_desc_lines

    @max_desc_lines.setter
    def max_desc_lines(self, value: int) -> None:
        self._picker.max_desc_lines = max(1, value)

    # --- content -------------------------------------------------------
    def set_items(self, items: list[PickerItem]) -> None:
        self._picker.set_items(items)
        self.checked = set()

    def set_filter(self, text: str) -> None:
        self._picker.set_filter(text)

    def get_selected_item(self) -> PickerItem | None:
        return self._picker.get_selected_item()

    def get_checked_items(self) -> list[PickerItem]:
        return [it for it in self._picker._all_items if it.value in self.checked]

    # --- navigation / actions -------------------------------------------
    def move_up(self) -> None:
        self._picker.move_up()

    def move_down(self) -> None:
        self._picker.move_down()

    def toggle(self) -> None:
        item = self._picker.get_selected_item()
        if item is None:
            return
        if item.value in self.checked:
            self.checked.discard(item.value)
        else:
            self.checked.add(item.value)

    def confirm(self) -> None:
        if self.on_confirm:
            self.on_confirm(self.get_checked_items())

    def cancel(self) -> None:
        if self.on_cancel:
            self.on_cancel()

    def handle_key(self, key: str) -> bool:
        if key == "up":
            self.move_up()
            return True
        if key == "down":
            self.move_down()
            return True
        if key == " " or key == "space":
            self.toggle()
            return True
        if key == "enter":
            self.confirm()
            return True
        if key == "escape":
            self.cancel()
            return True
        return False

    def invalidate(self) -> None:
        self._picker.invalidate()

    # --- rendering -------------------------------------------------------
    def render(self, width: int) -> list[str]:
        inner_width = max(1, width - 2)  # 2 border columns, see wrap_in_border
        if not self._picker.items:
            return wrap_in_border([self.theme.no_match("  No matches")], width)

        lines: list[str] = []
        primary_width = self._picker._primary_column_width()
        picker = self._picker
        start = max(0, min(picker.selected_index - picker.max_visible // 2,
                            len(picker.items) - picker.max_visible))
        end = min(start + picker.max_visible, len(picker.items))
        for i in range(start, end):
            lines.extend(self._render_item(picker.items[i], i == picker.selected_index, inner_width, primary_width))
        if start > 0 or end < len(picker.items):
            lines.append(self.theme.scroll_info(f"  ({picker.selected_index + 1}/{len(picker.items)})"))
        return wrap_in_border(lines, width)

    def _render_item(self, item: PickerItem, is_cursor: bool, width: int, primary_width: int) -> list[str]:
        box = "[x]" if item.value in self.checked else "[ ]"
        arrow = "\u2192 " if is_cursor else "  "
        prefix = f"{arrow}{box} "
        prefix_w = len(prefix)
        indent = " " * prefix_w
        body_lines = self._picker._render_item(item, False, max(1, width - prefix_w + 2), primary_width)
        lines = []
        for i, body in enumerate(body_lines):
            # _render_item already prepends its own 2-space "  " prefix; strip
            # it and substitute ours (arrow + checkbox) on the first line, or
            # our own matching indent on a wrapped continuation line.
            stripped = body[2:] if body.startswith("  ") else body
            line = f"{prefix}{stripped}" if i == 0 else f"{indent}{stripped}"
            if is_cursor:
                lines.append(self.theme.selected_text(line))
            elif item.value in self.checked:
                lines.append(self.theme.checked(line))
            else:
                lines.append(line)
        return lines
