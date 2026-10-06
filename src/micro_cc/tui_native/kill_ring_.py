"""Emacs-style kill ring: consecutive kills accumulate into one entry; supports yank and yank-pop."""


class KillRing:
    def __init__(self) -> None:
        self._ring: list[str] = []

    def push(self, text: str, *, prepend: bool, accumulate: bool = False) -> None:
        """Add text; accumulate merges into the latest entry (prepend for backward kills)."""
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
