"""Versioned system prompt: the head stays as the conversation began; later changes ride in the transcript as patches.

Rewriting the head (a CLAUDE.md edit, a new day) voids every prior thinking signature and the prompt cache.
"""

HEAD_KEY = "_system_head"  # on the query message that started tracking: the full sections
PATCH_KEY = "_system_patch"  # on a hidden reminder message: changed sections, None = removed

_LABELS = {
    "date": "Date",
    "orchestration": "Orchestration",
    "skills": "Skills",
    "claude_md": "Project instructions (CLAUDE.md)",
    "memory": "Long-term memory index",
}


def _apply(state: dict, patch: dict) -> dict:
    state = dict(state)
    for name, text in patch.items():
        if text is None:
            state.pop(name, None)
        else:
            state[name] = text
    return state


def applied(msgs: list) -> dict | None:
    """Sections the model has been told so far: head plus every patch; None before tracking starts."""
    state = None
    for m in msgs:
        if not isinstance(m, dict):
            continue
        if HEAD_KEY in m:
            state = dict(m[HEAD_KEY])
        elif PATCH_KEY in m and state is not None:
            state = _apply(state, m[PATCH_KEY])
    return state


def _diff(previous: dict, current: dict) -> dict:
    patch = {name: text for name, text in current.items() if previous.get(name) != text}
    patch.update({name: None for name in previous if name not in current})
    return patch


def _patch_text(patch: dict) -> str:
    parts = [f"## {_LABELS.get(name, name)}\n{text.strip() if text else '(removed)'}" for name, text in patch.items()]
    return (
        "<system-reminder>\nThese system prompt sections changed since this conversation started; "
        "the versions below replace the ones in the system prompt.\n\n"
        + "\n\n".join(parts)
        + "\n</system-reminder>"
    )


def sync(msgs: list, sections: dict, query_msg: dict) -> None:
    """Before the turn's query is appended: start the head on it, or append a patch for what changed."""
    state = applied(msgs)
    if state is None:
        query_msg[HEAD_KEY] = dict(sections)
        return
    patch = _diff(state, sections)
    if patch:
        msgs.append({"role": "user", "content": _patch_text(patch), PATCH_KEY: patch})


def head(msgs: list, window: list) -> dict:
    """Head as sent: start sections plus the patches trimmed out of the window, oldest first."""
    fold = sum(isinstance(m, dict) and PATCH_KEY in m for m in msgs) - sum(PATCH_KEY in m for m in window)
    state = {}
    for m in msgs:
        if not isinstance(m, dict):
            continue
        if HEAD_KEY in m:
            state = dict(m[HEAD_KEY])
        elif PATCH_KEY in m and fold > 0:
            state = _apply(state, m[PATCH_KEY])
            fold -= 1
    return state
