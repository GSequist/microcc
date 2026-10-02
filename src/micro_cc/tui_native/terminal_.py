"""Terminal driver for raw mode enter/exit, SIGWINCH-driven resize, bracketed
paste, Kitty keyboard protocol negotiation, and low-level write primitives
(cursor, clear, title, OSC 9;4 progress).

Windows is out of scope (this repo is macOS + Linux only).

The read loop owns the single StdinBuffer instance and `select()`/`os.read()`
loop, calling `StdinBuffer.complete_sequences()` once per tick. ProcessTerminal.
feed_stdin() is the seam: the read loop hands each already-segmented sequence
from that generator to `feed_stdin()` first, and only continues on to
`keys_.parse_key()` / the focused component when it returns False (not consumed
as part of Kitty negotiation).

setKittyProtocolActive()/is_kitty_protocol_active() state lives in keys_.py
(parse_key/matches_key read it to decide legacy-vs-Kitty decoding) and is
reused here.
"""

import math
import os
import re
import select
import shutil
import signal
import sys
import termios
import threading
import time
import tty
from typing import Callable, Protocol

from micro_cc.tui_native.keys_ import set_kitty_protocol_active

# --- sequences genuinely new here (not already in alt_screen_.py) --------

BRACKETED_PASTE_ENABLE = "\x1b[?2004h"
BRACKETED_PASTE_DISABLE = "\x1b[?2004l"

# Never enabled anywhere before this — without it the terminal simply
# never sends mouse bytes at all, which is the actual reason click-to-
# expand (and drag-select) looked broken: nothing was wrong with the
# dispatch logic, no mouse event ever arrived to dispatch. 1000 = basic
# click tracking, 1002 = also report motion while a button is held (needed
# for drag-select; 1003 would report ALL motion, unneeded and noisy),
# 1006 = SGR extended coordinates — alt_screen_.TuiAltScreen.
# parse_sgr_mouse_event() and stdin_buffer_'s completeness detection both
# assume this exact "\x1b[<...M/m" shape, not the legacy limited-range one.
MOUSE_TRACKING_ENABLE = "\x1b[?1000h\x1b[?1002h\x1b[?1006h"
MOUSE_TRACKING_DISABLE = "\x1b[?1006l\x1b[?1002l\x1b[?1000l"

ENABLE_MODIFY_OTHER_KEYS = "\x1b[>4;2m"
DISABLE_MODIFY_OTHER_KEYS = "\x1b[>4;0m"

DESIRED_KITTY_KEYBOARD_PROTOCOL_FLAGS = 7
KITTY_KEYBOARD_PROTOCOL_QUERY = f"\x1b[>{DESIRED_KITTY_KEYBOARD_PROTOCOL_FLAGS}u\x1b[?u\x1b[c"
DISABLE_KITTY_KEYBOARD_PROTOCOL = "\x1b[<u"
KEYBOARD_PROTOCOL_RESPONSE_FRAGMENT_TIMEOUT_S = 0.15

NATIVE_SHIFT_ENTER_SEQUENCE = "\x1b[13;2u"

TERMINAL_PROGRESS_ACTIVE_SEQUENCE = "\x1b]9;4;3\x07"
TERMINAL_PROGRESS_CLEAR_SEQUENCE = "\x1b]9;4;0\x07"
TERMINAL_PROGRESS_KEEPALIVE_S = 1.0

DEFAULT_ESCAPE_TIMEOUT_MS = 10
DEFAULT_SSH_ESCAPE_TIMEOUT_MS = 100

OnInput = Callable[[str], None]
OnResize = Callable[[], None]

# (type, flags) for "kitty-flags", (type, None) for "device-attributes".
KeyboardProtocolNegotiation = tuple[str, int | None]


def _terminal_size() -> tuple[int, int]:
    """Same call as TuiAltScreen._terminal_size in alt_screen_.py — kept
    as a free function here rather than imported since that one is bound
    to a TuiAltScreen instance, not reusable standalone."""
    size = shutil.get_terminal_size(fallback=(80, 24))
    return size.columns, size.lines


def parse_keyboard_protocol_negotiation_sequence(sequence: str) -> KeyboardProtocolNegotiation | None:
    kitty_flags = re.match(r"^\x1b\[\?(\d+)u$", sequence)
    if kitty_flags:
        return ("kitty-flags", int(kitty_flags.group(1)))
    if re.match(r"^\x1b\[\?[\d;]*c$", sequence):
        return ("device-attributes", None)
    return None


def _is_keyboard_protocol_negotiation_sequence_prefix(sequence: str) -> bool:
    return sequence == "\x1b[" or bool(re.match(r"^\x1b\[\?[\d;]*$", sequence))


def is_apple_terminal_session() -> bool:
    return sys.platform == "darwin" and os.environ.get("TERM_PROGRAM") == "Apple_Terminal"


def normalize_native_shift_enter_input(data: str, should_detect_native_shift_enter: bool, is_shift_pressed: bool) -> str:
    if should_detect_native_shift_enter and data == "\r" and is_shift_pressed:
        return NATIVE_SHIFT_ENTER_SEQUENCE
    return data


def normalize_apple_terminal_input(data: str, is_apple_terminal: bool, is_shift_pressed: bool) -> str:
    return normalize_native_shift_enter_input(data, is_apple_terminal, is_shift_pressed)


def resolve_escape_timeout_ms(env: dict | None = None) -> int:
    """How long to wait for the rest of an escape sequence before
    dispatching a lone ESC as the Escape key. Legacy Alt+key input is ESC
    plus another byte, so high-latency transports need a longer
    reassembly window."""
    if env is None:
        env = os.environ
    raw = env.get("PI_TUI_ESC_TIMEOUT")
    if raw is not None:
        try:
            configured = float(raw)
        except ValueError:
            configured = None
        if configured is not None and math.isfinite(configured) and configured > 0:
            return int(configured)
    if env.get("SSH_CONNECTION") or env.get("SSH_TTY"):
        return DEFAULT_SSH_ESCAPE_TIMEOUT_MS
    return DEFAULT_ESCAPE_TIMEOUT_MS


def _is_shift_pressed() -> bool:
    """terminal.ts asks native-modifiers.ts's isNativeModifierPressed()
    here — a native addon polling live OS keyboard-modifier state, with
    no stdlib equivalent. Not ported: unverified, always False, which
    makes forward_input_sequence's Apple Terminal Shift+Enter rewrite
    inert until that native hook exists in Python too."""
    return False


class Terminal(Protocol):
    """Minimal terminal interface for the native TUI — Python mirror of
    terminal.ts's exported `Terminal` interface."""

    def start(self, on_input: OnInput, on_resize: OnResize) -> None: ...
    def stop(self) -> None: ...
    def drain_input(self, max_ms: int = 1000, idle_ms: int = 50) -> None: ...
    def write(self, data: str) -> None: ...

    @property
    def columns(self) -> int: ...
    @property
    def rows(self) -> int: ...
    @property
    def kitty_protocol_active(self) -> bool: ...

    def move_by(self, lines: int) -> None: ...
    def hide_cursor(self) -> None: ...
    def show_cursor(self) -> None: ...
    def clear_line(self) -> None: ...
    def clear_from_cursor(self) -> None: ...
    def clear_screen(self) -> None: ...
    def set_title(self, title: str) -> None: ...
    def set_progress(self, active: bool) -> None: ...


class ProcessTerminal:
    """Real terminal using sys.stdin/sys.stdout."""

    def __init__(self) -> None:
        self._saved_termios = None
        self._input_handler: OnInput | None = None
        self._resize_handler: OnResize | None = None
        self._kitty_protocol_active = False
        self._modify_other_keys_active = False
        self._keyboard_protocol_pushed = False
        self._keyboard_protocol_negotiation_buffer = ""
        self._keyboard_protocol_buffer_flush_timer: threading.Timer | None = None
        self._progress_timer: threading.Timer | None = None

    @property
    def kitty_protocol_active(self) -> bool:
        return self._kitty_protocol_active

    @property
    def modify_other_keys_active(self) -> bool:
        return self._modify_other_keys_active

    @property
    def columns(self) -> int:
        return _terminal_size()[0]

    @property
    def rows(self) -> int:
        return _terminal_size()[1]

    # --- lifecycle ---------------------------------------------------

    def start(self, on_input: OnInput, on_resize: OnResize) -> None:
        self._input_handler = on_input
        self._resize_handler = on_resize

        if sys.stdin.isatty():
            try:
                self._saved_termios = termios.tcgetattr(sys.stdin.fileno())
                tty.setraw(sys.stdin.fileno())
            except termios.error:
                self._saved_termios = None

        self.write(BRACKETED_PASTE_ENABLE)
        self.write(MOUSE_TRACKING_ENABLE)

        if hasattr(signal, "SIGWINCH"):
            signal.signal(signal.SIGWINCH, self._on_sigwinch)
            # Refresh terminal dimensions immediately — they may be stale
            # after suspend/resume (SIGWINCH is lost while stopped).
            os.kill(os.getpid(), signal.SIGWINCH)

        self._query_and_enable_kitty_protocol()

    def stop(self) -> None:
        if self._clear_progress_timer():
            self.write(TERMINAL_PROGRESS_CLEAR_SEQUENCE)

        self.write(BRACKETED_PASTE_DISABLE)
        self.write(MOUSE_TRACKING_DISABLE)

        should_disable_kitty = self._keyboard_protocol_pushed or self._kitty_protocol_active
        self._clear_keyboard_protocol_negotiation_buffer()
        if should_disable_kitty:
            self.write(DISABLE_KITTY_KEYBOARD_PROTOCOL)
            self._keyboard_protocol_pushed = False
            self._kitty_protocol_active = False
            set_kitty_protocol_active(False)
        self._disable_modify_other_keys()

        self._input_handler = None
        if self._resize_handler is not None and hasattr(signal, "SIGWINCH"):
            signal.signal(signal.SIGWINCH, signal.SIG_DFL)
        self._resize_handler = None

        # process.stdin.pause() has no equivalent here: this class never
        # owned an active read loop in the first place (see module
        # docstring) — whichever loop is reading fd 0 is responsible for
        # stopping before/around this call.

        if self._saved_termios is not None:
            try:
                termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, self._saved_termios)
            except termios.error:
                pass
            self._saved_termios = None

    def drain_input(self, max_ms: int = 1000, idle_ms: int = 50) -> None:
        """Drain stdin before exiting to prevent Kitty key release events
        from leaking to the parent shell over slow SSH connections.
        Blocking/synchronous (there is no event-driven stdin stream to
        subscribe to here) — call right before process exit."""
        should_disable_kitty = self._keyboard_protocol_pushed or self._kitty_protocol_active
        self._clear_keyboard_protocol_negotiation_buffer()
        if should_disable_kitty:
            self.write(DISABLE_KITTY_KEYBOARD_PROTOCOL)
            self._keyboard_protocol_pushed = False
            self._kitty_protocol_active = False
            set_kitty_protocol_active(False)
        self._disable_modify_other_keys()

        previous_handler = self._input_handler
        self._input_handler = None

        fd = sys.stdin.fileno()
        last_data_time = time.monotonic()
        end_time = last_data_time + max_ms / 1000
        try:
            while True:
                now = time.monotonic()
                time_left = end_time - now
                if time_left <= 0:
                    break
                if now - last_data_time >= idle_ms / 1000:
                    break
                ready, _, _ = select.select([fd], [], [], min(idle_ms / 1000, time_left))
                if ready:
                    try:
                        os.read(fd, 4096)
                    except OSError:
                        pass
                    last_data_time = time.monotonic()
        finally:
            self._input_handler = previous_handler

    # --- Kitty keyboard protocol negotiation --------------------------
    #
    # Query terminal for Kitty keyboard protocol support and enable it if
    # available. Kitty's progressive enhancement detection requires
    # requesting the desired flags before querying them. The trailing DA
    # query is a sentinel supported by terminals that do not know Kitty
    # keyboard protocol; receiving DA before a Kitty response enables
    # modifyOtherKeys fallback without a startup timeout.
    #
    # Requested flags: 1 = disambiguate escape codes, 2 = report event
    # types (press/repeat/release), 4 = report alternate keys.

    def _query_and_enable_kitty_protocol(self) -> None:
        self._keyboard_protocol_pushed = True
        self._clear_keyboard_protocol_negotiation_buffer()
        self.write(KITTY_KEYBOARD_PROTOCOL_QUERY)

    def feed_stdin(self, sequence: str) -> bool:
        """Entry point for one already-segmented input sequence (see
        module docstring for who is expected to call this and with what).
        Returns True if the sequence was consumed as part of Kitty
        protocol negotiation (caller should not process it further),
        False if it was forwarded to the on_input handler."""
        negotiation = self._read_keyboard_protocol_negotiation_sequence(sequence)
        if negotiation == "pending":
            self._schedule_keyboard_protocol_negotiation_buffer_flush()
            return True
        if self._handle_keyboard_protocol_negotiation_sequence(negotiation):
            return True
        self._forward_input_sequence(sequence)
        return False

    def _read_keyboard_protocol_negotiation_sequence(self, sequence: str) -> KeyboardProtocolNegotiation | str | None:
        if self._keyboard_protocol_negotiation_buffer:
            buffered = self._keyboard_protocol_negotiation_buffer + sequence
            negotiation = parse_keyboard_protocol_negotiation_sequence(buffered)
            if negotiation is not None:
                self._clear_keyboard_protocol_negotiation_buffer()
                return negotiation
            if _is_keyboard_protocol_negotiation_sequence_prefix(buffered):
                self._set_keyboard_protocol_negotiation_buffer(buffered)
                return "pending"
            self._flush_keyboard_protocol_negotiation_buffer_as_input()

        negotiation = parse_keyboard_protocol_negotiation_sequence(sequence)
        if negotiation is not None:
            return negotiation
        if _is_keyboard_protocol_negotiation_sequence_prefix(sequence):
            self._set_keyboard_protocol_negotiation_buffer(sequence)
            return "pending"
        return None

    def _handle_keyboard_protocol_negotiation_sequence(self, negotiation: KeyboardProtocolNegotiation | None) -> bool:
        if negotiation is None:
            return False
        self._clear_keyboard_protocol_negotiation_buffer()
        kind, flags = negotiation
        if kind == "kitty-flags":
            if flags != 0:
                self._disable_modify_other_keys()
                if not self._kitty_protocol_active:
                    self._kitty_protocol_active = True
                    set_kitty_protocol_active(True)
            else:
                self._enable_modify_other_keys()
            return True

        if not self._kitty_protocol_active:
            self._enable_modify_other_keys()
        return True

    def _set_keyboard_protocol_negotiation_buffer(self, sequence: str) -> None:
        self._clear_keyboard_protocol_negotiation_buffer_flush_timer()
        self._keyboard_protocol_negotiation_buffer = sequence

    def _clear_keyboard_protocol_negotiation_buffer(self) -> None:
        self._clear_keyboard_protocol_negotiation_buffer_flush_timer()
        self._keyboard_protocol_negotiation_buffer = ""

    def _flush_keyboard_protocol_negotiation_buffer_as_input(self) -> None:
        if not self._keyboard_protocol_negotiation_buffer:
            return
        sequence = self._keyboard_protocol_negotiation_buffer
        self._clear_keyboard_protocol_negotiation_buffer()
        self._forward_input_sequence(sequence)

    def _schedule_keyboard_protocol_negotiation_buffer_flush(self) -> None:
        if not self._keyboard_protocol_negotiation_buffer or self._keyboard_protocol_buffer_flush_timer is not None:
            return
        timer = threading.Timer(KEYBOARD_PROTOCOL_RESPONSE_FRAGMENT_TIMEOUT_S, self._on_negotiation_buffer_flush_timer)
        timer.daemon = True
        self._keyboard_protocol_buffer_flush_timer = timer
        timer.start()

    def _on_negotiation_buffer_flush_timer(self) -> None:
        self._keyboard_protocol_buffer_flush_timer = None
        self._flush_keyboard_protocol_negotiation_buffer_as_input()

    def _clear_keyboard_protocol_negotiation_buffer_flush_timer(self) -> None:
        if self._keyboard_protocol_buffer_flush_timer is None:
            return
        self._keyboard_protocol_buffer_flush_timer.cancel()
        self._keyboard_protocol_buffer_flush_timer = None

    def _forward_input_sequence(self, sequence: str) -> None:
        if self._input_handler is None:
            return
        should_detect_native_shift_enter = sequence == "\r" and is_apple_terminal_session()
        is_shift_pressed = should_detect_native_shift_enter and _is_shift_pressed()
        data = normalize_native_shift_enter_input(sequence, should_detect_native_shift_enter, is_shift_pressed)
        self._input_handler(data)

    def _enable_modify_other_keys(self) -> None:
        if self._kitty_protocol_active or self._modify_other_keys_active:
            return
        self.write(ENABLE_MODIFY_OTHER_KEYS)
        self._modify_other_keys_active = True

    def _disable_modify_other_keys(self) -> None:
        if not self._modify_other_keys_active:
            return
        self.write(DISABLE_MODIFY_OTHER_KEYS)
        self._modify_other_keys_active = False

    def _on_sigwinch(self, signum, frame) -> None:
        if self._resize_handler is not None:
            self._resize_handler()

    # --- output --------------------------------------------------------

    def write(self, data: str) -> None:
        sys.stdout.write(data)
        sys.stdout.flush()

    def move_by(self, lines: int) -> None:
        if lines > 0:
            self.write(f"\x1b[{lines}B")
        elif lines < 0:
            self.write(f"\x1b[{-lines}A")

    def hide_cursor(self) -> None:
        self.write("\x1b[?25l")

    def show_cursor(self) -> None:
        self.write("\x1b[?25h")

    def clear_line(self) -> None:
        self.write("\x1b[K")

    def clear_from_cursor(self) -> None:
        self.write("\x1b[J")

    def clear_screen(self) -> None:
        self.write("\x1b[2J\x1b[H")

    def set_title(self, title: str) -> None:
        self.write(f"\x1b]0;{title}\x07")

    def set_progress(self, active: bool) -> None:
        if active:
            self.write(TERMINAL_PROGRESS_ACTIVE_SEQUENCE)
            if self._progress_timer is None:
                self._schedule_progress_tick()
        else:
            self._clear_progress_timer()
            self.write(TERMINAL_PROGRESS_CLEAR_SEQUENCE)

    def _schedule_progress_tick(self) -> None:
        timer = threading.Timer(TERMINAL_PROGRESS_KEEPALIVE_S, self._on_progress_tick)
        timer.daemon = True
        self._progress_timer = timer
        timer.start()

    def _on_progress_tick(self) -> None:
        self.write(TERMINAL_PROGRESS_ACTIVE_SEQUENCE)
        self._schedule_progress_tick()

    def _clear_progress_timer(self) -> bool:
        if self._progress_timer is None:
            return False
        self._progress_timer.cancel()
        self._progress_timer = None
        return True
