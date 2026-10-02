"""Native PromptInput — a text input component with editing capabilities.
Shaped to match what the OLD Textual PromptInput (start_live_.py:59-172,
now dead) actually did,
using reusable utilities like word_navigation_.find_word_backward/forward,
kill_ring_.KillRing, and undo_stack_.UndoStack. Full-featured editor
implementations with autocomplete pipelines are out of scope; at-picker
and slash-picker already live at the MicroTui level.

Satisfies the Component protocol (render/invalidate) plus a handle_key(
key_id) -> bool method — same convention as ListPicker/MultiSelectList,
not Textual's _on_key(event). MicroTui's (not yet built) read loop is
what will call handle_key once Phase 5 lands; nothing does yet.

At/slash-picker arrow-key interception does NOT live here (see the old
PromptInput._on_key's at_picker/slash-picker checks) — the plan says
that's better owned by whatever routes keystrokes once a focused-
component pointer exists (MicroTui.get_focus()), since this class has no
reason to know those pickers exist. handle_key only knows about this
input box's own editing.
"""

from micro_cc.tui_native.alt_screen_ import CURSOR_MARKER
from micro_cc.tui_native.kill_ring_ import KillRing
from micro_cc.tui_native.text_ import wrap_text_with_ansi
from micro_cc.tui_native.text_utils_ import visible_width
from micro_cc.tui_native.undo_stack_ import UndoStack
from micro_cc.tui_native.word_navigation_ import find_word_backward, find_word_forward
from micro_cc.utils import command_registry
from micro_cc.utils.terminal_setup import mark_multiline_used


def _wrap_line(line: str, width: int) -> list[str]:
    # strip_trailing=False — see wrap_text_with_ansi's docstring. This is
    # live, editable text, not a read-only render: a trailing space the
    # user just typed at a wrap boundary is real content, and the cursor
    # can currently sit right after it.
    return wrap_text_with_ansi(line, max(1, width), strip_trailing=False) or [""]


class PromptInput:
    _PASTE_THRESH = 200

    # Derived from command_registry.py — the single source of truth also
    # read by the GUI's command palette, so a command added there shows
    # up here for free instead of needing its own entry.
    SLASH_COMMAND_INFO = [(c["name"], c["hint"]) for c in command_registry.COMMANDS]
    SLASH_COMMANDS = [cmd for cmd, _ in SLASH_COMMAND_INFO]

    def __init__(self):
        self.lines: list[str] = [""]
        self.cursor_row = 0
        self.cursor_col = 0
        self.undo = UndoStack()
        self.kill_ring = KillRing()
        self.suggestion = ""
        self._placeholder = ""
        self.focused = False  # MicroTui.set_focus() flips this — only the
        # focused component should render a cursor marker.

        # Set by whoever owns this instance (MicroTui): fires with the
        # submitted text on Enter, and after every edit (replaces the dead
        # Textual on_text_area_changed message) so slash/at-picker
        # filtering can react.
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
        # Every /login, /keys, ask-user, /gui etc. call site sets this via
        # plain attribute assignment — none of them go through insert()/
        # clear()/_changed(), so without invalidating here the render cache
        # (keyed only on width, populated by whatever last called render())
        # keeps serving its stale cached lines and the new placeholder
        # never appears until some unrelated keystroke happens to
        # invalidate it first.
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
        """Cmd+Backspace on macOS — kill from line start to the cursor,
        same kill-ring semantics as delete_word_left."""
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
        """Replace the range [start, end) — both (row, col) — with `text`.
        Only ever called today with start/end on the same row (@-mention
        replacement), so that's the only case implemented."""
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
        """Intercept large pastes with a placeholder, pass short ones
        through. Same threshold/marker format as the old Textual version
        so the ⟪paste:N|...⟫ -> real-content substitution already in
        on_prompt_input_submitted keeps working unchanged."""
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
            matches = [c for c in self.SLASH_COMMANDS if c.startswith(text)]
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
            # Universal newline fallback with zero terminal config: a
            # literal backslash then Enter.
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
            # Cmd+Left/Right ("jump to start/end of line" on macOS) never
            # arrives as a Home/End key — it's the physical Left/Right
            # arrow with the super modifier bit set (keys_.py decodes it
            # to this key_id correctly already), not a terminal-level
            # translation to Home/End. Only the literal Home/End keys
            # were wired below; this was the missing branch.
            self.cursor_home()
            return True
        if key_id == "super+right":
            self.cursor_end()
            return True
        if key_id == "ctrl+a":
            # Cmd+Left on Ghostty/kitty the "home" convention: those terminals
            # translate it to a raw \x01 (ctrl+a) BEFORE it ever reaches us
            # (Ghostty default keybind `super+arrow_left = text:\x01`), so the
            # super+left branch above never fires there. Same "move to start
            # of line" intent, different wire form — wire both.
            self.cursor_home()
            return True
        if key_id == "ctrl+e":
            # Cmd+Right -> \x05 (ctrl+e) on Ghostty (`super+arrow_right =
            # text:\x05`) is the mirror of ctrl+a above; see that comment.
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
            # Cmd+Delete on Ghostty (`super+backspace = text:\x15`, i.e. raw
            # ctrl+u) — the super+backspace branch above never sees it there.
            # Same kill-to-line-start semantics as macOS Cmd+Delete, so it
            # shares delete_to_line_start() (also the ctrl+u convention in
            # readline/emacs).
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
            # parse_key() names the space bar "space", not a literal " " —
            # every other printable character comes through as itself, but
            # this one didn't match the len(key_id) == 1 fallback below.
            self.insert(" ")
            return True
        if key_id == "tab":
            # No suggestion to accept and no picker open (that case is
            # intercepted before this ever gets called — see
            # start_live_tui_._on_terminal_input's live_picker routing) —
            # a bare Tab with nothing to do is a no-op, not a character.
            return True
        if len(key_id) == 1:
            self.insert(key_id)
            return True
        return False

    # --- Component protocol --------------------------------------------
    def invalidate(self) -> None:
        self._cached_width = None
        self._cached_lines = None

    # Bold "›" + space — same marker MessageRow already uses for a
    # submitted user message (Text(f"› {content}", style="bold")) — so a
    # message reads the same way whether it's sitting in the transcript or
    # still being typed. Wrapped lines after the first get two plain
    # spaces instead, so continuation text lines up under the marker.
    _MARKER = "\x1b[1m›\x1b[0m "
    _MARKER_WIDTH = 2
    _CONTINUATION = "  "

    # Old Textual PromptInput's own CSS (microcc-styles.tcss, now dead)
    # capped this at max-height: 8. root.add(bottom_bar, ..., shrink=0) in
    # start_live_tui_.py means bottom_bar never gives up rows under
    # pressure — only messages_scroll can shrink, down to 0 — so an
    # unbounded prompt (e.g. many manual newlines) has nothing above it
    # to absorb the overflow and pushes the banner off-screen instead.
    # Same cap, ported forward.
    MAX_VISIBLE_LINES = 8

    def render(self, width: int) -> list[str]:
        if self._cached_width == width and self._cached_lines is not None:
            return self._cached_lines

        content_width = max(1, width - self._MARKER_WIDTH)

        if not self.text and self.placeholder:
            # Dimmed, same convention as the ghost-text suggestion below —
            # without this a placeholder is indistinguishable from real
            # typed content (same bold marker, full-weight text).
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
                # Ghost-text autocomplete — update_suggestion() already
                # computes self.suggestion on every edit, but nothing ever
                # rendered it (Textual's TextArea did this automatically
                # via its own .suggestion property; that's gone along with
                # TextArea, so it has to be drawn explicitly here).
                suggestion = f"\x1b[2m{self.suggestion}\x1b[0m" if self.suggestion else ""
                out[cursor_render_row] = r[:cursor_render_col] + CURSOR_MARKER + suggestion + r[cursor_render_col:]

            # Window to MAX_VISIBLE_LINES, centered on the cursor's render
            # row — same scrolling-window pattern as ListPicker.render()
            # (list_picker_.py), just keyed on the cursor instead of a
            # selected_index.
            anchor = cursor_render_row if cursor_render_row is not None else len(out) - 1
            start = max(0, min(anchor - self.MAX_VISIBLE_LINES // 2, len(out) - self.MAX_VISIBLE_LINES))
            start = max(0, start)

        end = min(start + self.MAX_VISIBLE_LINES, len(out))
        out = out[start:end]
        prefixes = [self._MARKER if start == 0 else self._CONTINUATION]
        prefixes += [self._CONTINUATION] * (len(out) - 1)

        lines = [p + line for p, line in zip(prefixes, out)]

        self._cached_width = width
        self._cached_lines = lines
        return lines
