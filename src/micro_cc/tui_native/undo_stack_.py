"""Generic undo stack that deep-copies snapshots on push to isolate them from later mutation."""

import copy
from typing import Generic, TypeVar

S = TypeVar("S")


class UndoStack(Generic[S]):
    def __init__(self) -> None:
        self._stack: list[S] = []

    def push(self, state: S) -> None:
        """Push a deep clone of the given state onto the stack."""
        self._stack.append(copy.deepcopy(state))

    def pop(self) -> S | None:
        """Pop and return the most recent snapshot, or None if empty."""
        return self._stack.pop() if self._stack else None

    def clear(self) -> None:
        """Remove all snapshots."""
        self._stack.clear()

    @property
    def length(self) -> int:
        return len(self._stack)
