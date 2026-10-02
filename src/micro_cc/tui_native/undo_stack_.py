"""Generic undo stack with clone-on-push semantics. Stores deep copies of
state snapshots, so later in-place mutation of the caller's live state can't
retroactively corrupt a snapshot already sitting on the stack. Popped snapshots
are handed back as-is (no re-copy needed — they're already detached from
whatever pushed them).

Implemented as a stateful class managing the stack."""

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
