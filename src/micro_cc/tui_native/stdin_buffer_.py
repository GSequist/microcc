"""Buffers raw stdin bytes and detects when a CSI/OSC/DCS/APC escape
sequence is complete before handing it onward. Without this, a sequence
that arrives split across multiple os.read() calls (e.g. an SGR mouse
event delivered as "\x1b[<" then "35;20;5m") gets misread as garbage
individual keystrokes instead of one event — the desync bug class this
whole native-TUI rewrite exists to fix.

Detects when escape sequences are complete before handing them onward,
implementing a completeness-detection state machine that buffers bytes until
a sequence is recognized as finished. The delivery mechanism is pull-based
rather than push-based with background timers, so `feed()` and
`complete_sequences()` each check whether
the pending-sequence deadline has already elapsed by wall clock and flush if so.
`feed()` treats an already-elapsed deadline as the timer firing before this
call arrived; `complete_sequences()` performs the same check with no new input,
which makes it safe for the read loop to poll every select() tick and still
observe a timeout-driven flush with no bytes arriving.

Inspired by designs from OpenTUI (https://github.com/sst/opentui),
MIT License.
"""

import re
import time
from dataclasses import dataclass

ESC = "\x1b"
DEFAULT_SEQUENCE_TIMEOUT_MS = 50
DEFAULT_ESCAPE_TIMEOUT_MS = 10
BRACKETED_PASTE_START = "\x1b[200~"
BRACKETED_PASTE_END = "\x1b[201~"

_CSI_MOUSE_RE = re.compile(r"^<\d+;\d+;\d+[Mm]$")
_KITTY_PRINTABLE_RE = re.compile(r"^\x1b\[(\d+)(?::\d*)?(?::\d+)?u$")


@dataclass(frozen=True)
class Paste:
    """A completed bracketed-paste block, yielded by complete_sequences()
    alongside plain str key sequences. Kept as its own type (rather than
    a plain str) so a consumer can tell "200 chars of pasted text" apart
    from "200 chars of individual keystrokes" with an isinstance check."""
    text: str


def _is_complete_csi_sequence(data: str) -> str:
    if not data.startswith(ESC + "["):
        return "complete"
    if len(data) < 3:
        return "incomplete"
    payload = data[2:]
    last_char = payload[-1]
    last_code = ord(last_char)
    if not (0x40 <= last_code <= 0x7E):
        return "incomplete"
    if payload.startswith("<"):
        # SGR mouse sequences (ESC[<B;X;Ym) need three ; separated numeric
        # fields before the terminal M/m — a lone trailing M/m byte is not
        # by itself proof the sequence is done (a numeric field could still
        # be mid-flight), so check the full shape.
        if _CSI_MOUSE_RE.match(payload):
            return "complete"
        if last_char in ("M", "m"):
            parts = payload[1:-1].split(";")
            if len(parts) == 3 and all(p.isdigit() for p in parts):
                return "complete"
        return "incomplete"
    return "complete"


def _is_complete_osc_sequence(data: str) -> str:
    if not data.startswith(ESC + "]"):
        return "complete"
    if data.endswith(ESC + "\\") or data.endswith("\x07"):
        return "complete"
    return "incomplete"


def _is_complete_dcs_sequence(data: str) -> str:
    if not data.startswith(ESC + "P"):
        return "complete"
    return "complete" if data.endswith(ESC + "\\") else "incomplete"


def _is_complete_apc_sequence(data: str) -> str:
    if not data.startswith(ESC + "_"):
        return "complete"
    return "complete" if data.endswith(ESC + "\\") else "incomplete"


def _is_complete_sequence(data: str) -> str:  # "complete" | "incomplete" | "not-escape"
    if not data.startswith(ESC):
        return "not-escape"
    if len(data) == 1:
        return "incomplete"
    after_esc = data[1:]
    if after_esc.startswith("["):
        if after_esc.startswith("[M"):
            # Old-style (non-SGR) mouse reporting: ESC[M + 3 raw bytes,
            # always exactly 6 bytes total, no terminator byte to look for.
            return "complete" if len(data) >= 6 else "incomplete"
        return _is_complete_csi_sequence(data)
    if after_esc.startswith("]"):
        return _is_complete_osc_sequence(data)
    if after_esc.startswith("P"):
        return _is_complete_dcs_sequence(data)
    if after_esc.startswith("_"):
        return _is_complete_apc_sequence(data)
    if after_esc.startswith("O"):
        return "complete" if len(after_esc) >= 2 else "incomplete"
    if len(after_esc) == 1:
        return "complete"  # meta key: ESC + one plain character
    return "complete"  # unrecognized shape — don't hold input hostage waiting for more


def _parse_unmodified_kitty_printable_codepoint(sequence: str) -> int | None:
    match = _KITTY_PRINTABLE_RE.match(sequence)
    if not match:
        return None
    codepoint = int(match.group(1))
    return codepoint if codepoint >= 32 else None


def _extract_complete_sequences(buffer: str) -> tuple[list[str], str]:
    sequences: list[str] = []
    pos = 0
    while pos < len(buffer):
        remaining = buffer[pos:]
        if not remaining.startswith(ESC):
            sequences.append(remaining[0])
            pos += 1
            continue

        seq_end = 1
        while seq_end <= len(remaining):
            candidate = remaining[:seq_end]
            status = _is_complete_sequence(candidate)
            if status == "complete":
                if candidate == ESC * 2:
                    # WezTerm with enable_kitty_keyboard sends the Escape
                    # keypress as a raw ESC and the release as a full Kitty
                    # CSI-u sequence, concatenated: "\x1b\x1b[27;...u". Left
                    # alone, "\x1b\x1b" reads as a complete meta-key
                    # sequence, leaving "[27;...u" to be typed as plain
                    # text. If what follows would start a new escape
                    # sequence, emit only the first ESC and restart from
                    # the second.
                    next_char = remaining[seq_end] if seq_end < len(remaining) else None
                    if next_char in ("[", "]", "O", "P", "_"):
                        sequences.append(ESC)
                        pos += 1
                        break
                sequences.append(candidate)
                pos += seq_end
                break
            if status == "incomplete":
                seq_end += 1
                continue
            sequences.append(candidate)  # not-escape: shouldn't happen when starting with ESC
            pos += seq_end
            break
        else:
            return sequences, remaining
    return sequences, ""


class StdinBuffer:
    def __init__(self, timeout_ms: float = DEFAULT_SEQUENCE_TIMEOUT_MS, escape_timeout_ms: float = DEFAULT_ESCAPE_TIMEOUT_MS):
        self._buffer = ""
        self._deadline: float | None = None
        self._timeout_ms = timeout_ms
        self._escape_timeout_ms = escape_timeout_ms
        self._paste_mode = False
        self._paste_buffer = ""
        self._pending_kitty_codepoint: int | None = None
        self._queue: list[str | Paste] = []

    def feed(self, data: str | bytes) -> None:
        self._maybe_flush_expired()
        self._deadline = None  # any pending deadline is superseded by this call
        self._process_str(self._decode(data))

    def complete_sequences(self):
        """Drain and yield every sequence/paste completed so far. Safe to
        call on every tick of a select() poll loop even with no new
        input — that's what lets a timeout-only flush (a lone ESC that
        never completes into anything, or an incomplete sequence past its
        deadline) surface with no further feed() call."""
        self._maybe_flush_expired()
        while self._queue:
            yield self._queue.pop(0)

    def flush(self) -> list[str]:
        """Force out whatever's currently buffered, as a single chunk,
        without running it back through the Kitty-duplicate check or
        queueing it for complete_sequences(). For callers that want the
        raw leftover directly (shutdown, tests) rather than through the
        normal delivery path."""
        self._deadline = None
        if not self._buffer:
            return []
        sequences = [self._buffer]
        self._buffer = ""
        self._pending_kitty_codepoint = None
        return sequences

    def clear(self) -> None:
        self._deadline = None
        self._buffer = ""
        self._paste_mode = False
        self._paste_buffer = ""
        self._pending_kitty_codepoint = None

    def destroy(self) -> None:
        self.clear()

    def get_buffer(self) -> str:
        return self._buffer

    # --- internals ---------------------------------------------------

    def _decode(self, data: str | bytes) -> str:
        if isinstance(data, (bytes, bytearray)):
            if len(data) == 1 and data[0] > 127:
                # High-bit-set single byte (legacy 8-bit meta encoding) ->
                # ESC + the same char with the high bit cleared, so it
                # parses the same as a "meta key" escape sequence.
                return ESC + chr(data[0] - 128)
            return bytes(data).decode("utf-8", errors="replace")
        return data

    def _process_str(self, s: str) -> None:
        if s == "" and self._buffer == "":
            self._emit_data("")
            return

        self._buffer += s

        if self._paste_mode:
            self._paste_buffer += self._buffer
            self._buffer = ""
            self._try_complete_paste()
            return

        start_index = self._buffer.find(BRACKETED_PASTE_START)
        if start_index != -1:
            if start_index > 0:
                # Any incomplete escape sequence directly abutting the
                # paste marker is dropped here, not held for later. Not
                # expected in practice: nothing sends a half-finished CSI
                # immediately before a paste marker.
                before_paste = self._buffer[:start_index]
                sequences, _ = _extract_complete_sequences(before_paste)
                for seq in sequences:
                    self._emit_data(seq)
            self._pending_kitty_codepoint = None
            self._buffer = self._buffer[start_index + len(BRACKETED_PASTE_START):]
            self._paste_mode = True
            self._paste_buffer = self._buffer
            self._buffer = ""
            self._try_complete_paste()
            return

        sequences, remainder = _extract_complete_sequences(self._buffer)
        self._buffer = remainder
        for seq in sequences:
            self._emit_data(seq)

        if self._buffer:
            wait_ms = self._escape_timeout_ms if self._buffer == ESC else self._timeout_ms
            self._deadline = time.monotonic() + wait_ms / 1000

    def _try_complete_paste(self) -> None:
        end_index = self._paste_buffer.find(BRACKETED_PASTE_END)
        if end_index == -1:
            return
        content = self._paste_buffer[:end_index]
        remaining = self._paste_buffer[end_index + len(BRACKETED_PASTE_END):]
        self._paste_mode = False
        self._paste_buffer = ""
        self._pending_kitty_codepoint = None
        self._queue.append(Paste(content))
        if remaining:
            self._process_str(remaining)

    def _emit_data(self, sequence: str) -> None:
        # Kitty's CSI-u protocol reports both a printable-key press and a
        # release; for an unmodified printable key, the release re-sends
        # the same codepoint as a raw character right after. Drop that
        # raw duplicate so a plain keystroke doesn't get typed twice.
        raw_codepoint = ord(sequence) if len(sequence) == 1 else None
        if raw_codepoint is not None and raw_codepoint == self._pending_kitty_codepoint:
            self._pending_kitty_codepoint = None
            return
        self._pending_kitty_codepoint = _parse_unmodified_kitty_printable_codepoint(sequence)
        self._queue.append(sequence)

    def _maybe_flush_expired(self) -> None:
        if self._deadline is not None and time.monotonic() >= self._deadline:
            self._deadline = None
            for seq in self.flush():
                self._emit_data(seq)
