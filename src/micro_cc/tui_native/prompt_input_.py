"""Text input component with editing, undo, kill ring, and word navigation."""

from micro_cc.tui_native.alt_screen_ import CURSOR_MARKER
from micro_cc.tui_native.glyphs_ import glyph
from micro_cc.tui_native.kill_ring_ import KillRing
from micro_cc.tui_native.text_ import wrap_text_with_ansi
from micro_cc.tui_native.text_utils_ import visible_width
from micro_cc.tui_native.undo_stack_ import UndoStack
from micro_cc.tui_native.word_navigation_ import find_word_backward, find_word_forward
from micro_cc import mods_
from micro_cc.utils.terminal_setup import mark_multiline_used


def _wrap_line(line: str, width: int) -> list[str]:
    # strip_trailing=False: trailing space at wrap boundary is real editable content.
    return wrap_text_with_ansi(line, max(1, width), strip_trailing=False) or [""]


class PromptInput:
    _PASTE_THRESH = 200

    def __init__(self):
        self.lines: list[str] = [""]
        self.cursor_row = 0
        self.cursor_col = 0
        self.undo = UndoStack()
        self.kill_ring = KillRing()
        self.suggestion = ""
        self._placeholder = ""
        self.focused = False
        # Set by MicroTui: on_submit fires on Enter, on_change fires on every edit for picker filtering.
        self.on_submit = None       # Callable[[str], None]
        self.on_change = None       # Callable[[str], None]

        self._paste_store: dict[int, str] = {}
        self._paste_id = 0

        self._cached_width: int | None = None
        self._cached_lines: list[str] | None = None

    # --- content ---------------------------------------------------------
    @property
    def text(self) -> str:
        return "\n".join(self.lines)

    @property
    def placeholder(self) -> str:
        return self._placeholder

    @placeholder.setter
    def placeholder(self, value: str) -> None:
        # Invalidate cache so new placeholder appears (cache keyed only on width).
        if value == self._placeholder:
            return
        self._placeholder = value
        self.invalidate()

    def _snapshot(self):
        return (list(self.lines), self.cursor_row, self.cursor_col)

    def _restore(self, snap) -> None:
        self.lines, self.cursor_row, self.cursor_col = snap[0][:], snap[1], snap[2]

    def _changed(self) -> None:
        self.invalidate()
        self.update_suggestion()
        if self.on_change:
            self.on_change(self.text)

    def clear(self) -> None:
        self.undo.push(self._snapshot())
        self.lines = [""]
        self.cursor_row = 0
        self.cursor_col = 0
        self._changed()

    def insert(self, s: str) -> None:
        if not s:
            return
        self.undo.push(self._snapshot())
        row, col = self.cursor_row, self.cursor_col
        before, after = self.lines[row][:col], self.lines[row][col:]
        parts = s.split("\n")
        if len(parts) == 1:
            self.lines[row] = before + s + after
            self.cursor_col += len(s)
        else:
            self.lines[row] = before + parts[0]
            self.lines[row + 1:row + 1] = parts[1:-1] + [parts[-1] + after]
            self.cursor_row += len(parts) - 1
            self.cursor_col = len(parts[-1])
        self._changed()

    def delete_left(self) -> None:
        row, col = self.cursor_row, self.cursor_col
        if col > 0:
            self.undo.push(self._snapshot())
            self.lines[row] = self.lines[row][:col - 1] + self.lines[row][col:]
            self.cursor_col -= 1
            self._changed()
        elif row > 0:
            self.undo.push(self._snapshot())
            prev_len = len(self.lines[row - 1])
            self.lines[row - 1] += self.lines.pop(row)
            self.cursor_row -= 1
            self.cursor_col = prev_len
            self._changed()

    def delete_word_left(self) -> None:
        row, col = self.cursor_row, self.cursor_col
        new_col = find_word_backward(self.lines[row], col)
        killed = self.lines[row][new_col:col]
        if not killed:
            self.delete_left()
            return
        self.undo.push(self._snapshot())
        self.kill_ring.push(killed, prepend=True)
        self.lines[row] = self.lines[row][:new_col] + self.lines[row][col:]
        self.cursor_col = new_col
        self._changed()

    def delete_to_line_start(self) -> None:
        """Kill from line start to cursor with kill-ring semantics."""
        row, col = self.cursor_row, self.cursor_col
        killed = self.lines[row][:col]
        if not killed:
            self.delete_left()
            return
        self.undo.push(self._snapshot())
        self.kill_ring.push(killed, prepend=True)
        self.lines[row] = self.lines[row][col:]
        self.cursor_col = 0
        self._changed()

    def yank(self) -> None:
        text = self.kill_ring.peek()
        if text:
            self.insert(text)

    def undo_edit(self) -> None:
        snap = self.undo.pop()
        if snap is not None:
            self._restore(snap)
            self._changed()

    # --- cursor motion -----------------------------------------------------
    def cursor_word_left(self) -> None:
        self.cursor_col = find_word_backward(self.lines[self.cursor_row], self.cursor_col)
        self.invalidate()

    def cursor_word_right(self) -> None:
        self.cursor_col = find_word_forward(self.lines[self.cursor_row], self.cursor_col)
        self.invalidate()

    def cursor_left(self) -> None:
        if self.cursor_col > 0:
            self.cursor_col -= 1
        elif self.cursor_row > 0:
            self.cursor_row -= 1
            self.cursor_col = len(self.lines[self.cursor_row])
        self.invalidate()

    def cursor_right(self) -> None:
        if self.cursor_col < len(self.lines[self.cursor_row]):
            self.cursor_col += 1
        elif self.cursor_row < len(self.lines) - 1:
            self.cursor_row += 1
            self.cursor_col = 0
        self.invalidate()

    def cursor_up(self) -> None:
        if self.cursor_row > 0:
            self.cursor_row -= 1
            self.cursor_col = min(self.cursor_col, len(self.lines[self.cursor_row]))
            self.invalidate()

    def cursor_down(self) -> None:
        if self.cursor_row < len(self.lines) - 1:
            self.cursor_row += 1
            self.cursor_col = min(self.cursor_col, len(self.lines[self.cursor_row]))
            self.invalidate()

    def cursor_home(self) -> None:
        self.cursor_col = 0
        self.invalidate()

    def cursor_end(self) -> None:
        self.cursor_col = len(self.lines[self.cursor_row])
        self.invalidate()

    def get_line(self, row: int) -> str:
        return self.lines[row]

    @property
    def cursor_location(self) -> tuple[int, int]:
        return self.cursor_row, self.cursor_col

    def replace(self, text: str, start: tuple[int, int], end: tuple[int, int]) -> None:
        """Replace range [start, end) with text; only same-row replacements implemented."""
        (sr, sc), (er, ec) = start, end
        if sr != er:
            raise NotImplementedError("multi-row replace not needed yet")
        self.undo.push(self._snapshot())
        line = self.lines[sr]
        self.lines[sr] = line[:sc] + text + line[ec:]
        self.cursor_row = sr
        self.cursor_col = sc + len(text)
        self._changed()

    # --- paste -------------------------------------------------------------
    def _handle_paste(self, text: str) -> None:
        """Large pastes get a placeholder; threshold/marker compatible with legacy system."""
        if len(text) > self._PASTE_THRESH:
            self._paste_id += 1
            pid = self._paste_id
            self._paste_store[pid] = text
            n_lines = text.count("\n") + 1
            self.insert(f"⟪paste:{pid}|{len(text)} chars, {n_lines} lines⟫")
        else:
            self.insert(text)

    # --- slash-command suggestion ------------------------------------------
    def update_suggestion(self) -> None:
        text = self.text
        if text.startswith("/") and "\n" not in text:
            matches = [c for c, _ in mods_.slash_command_info() if c.startswith(text)]
            if matches:
                self.suggestion = matches[0][len(text):]
                return
        self.suggestion = ""

    # --- keys ----------------------------------------------------------
    def handle_key(self, key_id: str) -> bool:
        if key_id in ("shift+enter", "alt+enter", "ctrl+j"):
            self.insert("\n")
            mark_multiline_used()
            return True
        if key_id == "tab" and self.suggestion:
            self.insert(self.suggestion)
            return True
        if key_id == "enter":
            row, col = self.cursor_row, self.cursor_col
            # Backslash-Enter -> newline (universal fallback, requires no terminal config).
            if col > 0 and self.lines[row][col - 1] == "\\":
                self.delete_left()
                self.insert("\n")
                mark_multiline_used()
                return True
            if self.on_submit:
                self.on_submit(self.text)
            return True
        if key_id == "alt+left":
            self.cursor_word_left()
            return True
        if key_id == "alt+right":
            self.cursor_word_right()
            return True
        if key_id == "super+left":
            # Cmd+Left on macOS arrives as super+left, not Home (keys_.py decodes this).
            self.cursor_home()
            return True
        if key_id == "super+right":
            self.cursor_end()
            return True
        if key_id == "ctrl+a":
            # Ghostty/kitty translate Cmd+Left to \x01 (ctrl+a) at terminal level.
            self.cursor_home()
            return True
        if key_id == "ctrl+e":
            # Ghostty/kitty translate Cmd+Right to \x05 (ctrl+e) at terminal level.
            self.cursor_end()
            return True
        if key_id in ("ctrl+h", "backspace"):
            self.delete_left()
            return True
        if key_id in ("ctrl+w", "alt+backspace"):
            self.delete_word_left()
            return True
        if key_id == "super+backspace":
            self.delete_to_line_start()
            return True
        if key_id == "ctrl+u":
            # Ghostty translates Cmd+Delete to \x15 (ctrl+u); share delete_to_line_start semantics.
            self.delete_to_line_start()
            return True
        if key_id == "ctrl+y":
            self.yank()
            return True
        if key_id == "ctrl+z":
            self.undo_edit()
            return True
        if key_id == "left":
            self.cursor_left()
            return True
        if key_id == "right":
            self.cursor_right()
            return True
        if key_id == "up":
            self.cursor_up()
            return True
        if key_id == "down":
            self.cursor_down()
            return True
        if key_id == "home":
            self.cursor_home()
            return True
        if key_id == "end":
            self.cursor_end()
            return True
        if key_id == "space":
            # parse_key() names space as "space", not " ", so explicit handler needed.
            self.insert(" ")
            return True
        if key_id == "tab":
            # Bare Tab with no suggestion or picker open is a no-op.
            return True
        if len(key_id) == 1:
            self.insert(key_id)
            return True
        return False

    # --- Component protocol --------------------------------------------
    def invalidate(self) -> None:
        self._cached_width = None
        self._cached_lines = None

    # Capped at 8 to prevent unbounded prompt from pushing banner off-screen.
    MAX_VISIBLE_LINES = 8

    def render(self, width: int) -> list[str]:
        if self._cached_width == width and self._cached_lines is not None:
            return self._cached_lines

        # Bold marker same as MessageRow, read per render so a mod glyph applies; continuation aligned under it.
        mark = glyph("user_prompt")
        marker_width = visible_width(mark) + 1
        content_width = max(1, width - marker_width)

        if not self.text and self.placeholder:
            # Dimmed placeholder to distinguish from real typed content.
            wrapped = _wrap_line(self.placeholder, content_width)
            out = [f"\x1b[2m{w}\x1b[0m" for w in wrapped]
            start = 0
        else:
            out = []
            cursor_render_row = cursor_render_col = None
            for row_idx, line in enumerate(self.lines):
                wrapped = _wrap_line(line, content_width)
                if row_idx == self.cursor_row:
                    acc = 0
                    for wi, wline in enumerate(wrapped):
                        wlen = visible_width(wline)
                        if self.cursor_col <= acc + wlen or wi == len(wrapped) - 1:
                            cursor_render_row = len(out) + wi
                            cursor_render_col = self.cursor_col - acc
                            break
                        acc += wlen
                out.extend(wrapped)
            if self.focused and cursor_render_row is not None:
                r = out[cursor_render_row]
                # Ghost-text autocomplete; must render explicitly since TextArea is gone.
                suggestion = f"\x1b[2m{self.suggestion}\x1b[0m" if self.suggestion else ""
                out[cursor_render_row] = r[:cursor_render_col] + CURSOR_MARKER + suggestion + r[cursor_render_col:]

            # Window to MAX_VISIBLE_LINES centered on cursor; same pattern as ListPicker.render().
            anchor = cursor_render_row if cursor_render_row is not None else len(out) - 1
            start = max(0, min(anchor - self.MAX_VISIBLE_LINES // 2, len(out) - self.MAX_VISIBLE_LINES))
            start = max(0, start)

        end = min(start + self.MAX_VISIBLE_LINES, len(out))
        out = out[start:end]
        cont = " " * marker_width
        prefixes = [f"{mark} " if start == 0 else cont]
        prefixes += [cont] * (len(out) - 1)

        lines = [p + line for p, line in zip(prefixes, out)]

        self._cached_width = width
        self._cached_lines = lines
        return lines
