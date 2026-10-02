"""Ring buffer for Emacs-style kill/yank operations. Tracks killed (deleted)
text entries; consecutive kills accumulate into a single entry (so a run of
ctrl+k presses yanks back as one block, not one ring entry per keystroke).
Supports yank (paste most recent) and yank-pop (cycle through older entries).

Implemented as a stateful class managing the ring buffer."""


class KillRing:
    def __init__(self) -> None:
        self._ring: list[str] = []

    def push(self, text: str, *, prepend: bool, accumulate: bool = False) -> None:
        """Add text to the kill ring.

        prepend: if accumulating, prepend (backward deletion) or append
        (forward deletion) — keeps repeated word-kills in either
        direction reading back in the order they were typed.
        accumulate: merge with the most recent entry instead of
        creating a new one.
        """
        if not text:
            return

        if accumulate and self._ring:
            last = self._ring.pop()
            self._ring.append(text + last if prepend else last + text)
        else:
            self._ring.append(text)

    def peek(self) -> str | None:
        """Most recent entry, without modifying the ring."""
        return self._ring[-1] if self._ring else None

    def rotate(self) -> None:
        """Move the most recent entry to the front, for yank-pop cycling."""
        if len(self._ring) > 1:
            self._ring.insert(0, self._ring.pop())

    @property
    def length(self) -> int:
        return len(self._ring)
