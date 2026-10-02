"""Live state for the GUI server — one project, one browser, one process.

The whole reason this file is short: `claude_loop` is an async generator that
*suspends* on `approval_request`/`question_asked` (it yields a mutable dict and
only resumes once the consumer fills it in). A multi-process server would have
to persist that suspended state externally and replay the assistant's content
blocks on the next request, because there the generator dies with the HTTP
response. Here the server and the loop share a process, so the suspended
generator object IS the pending state — we just hold onto it between requests
and keep iterating.
"""

import asyncio

from micro_cc.utils.msg_store_ import load_checkpoint

# One served project per server process, so this is a plain module-level dict
# rather than a keyed registry. Fields:
#   project_dir   str
#   msgs          list — the live transcript handed to claude_loop
#   gen           the suspended claude_loop async generator, or None
#   pending       the mutable dict claude_loop is blocked on
#                 ({"approved": None} or {"answered": None}), or None
#   pending_kind  "approval" | "question"
#   pending_tool  {"id", "name", "input"} — what's waiting, for re-render
#   answers       for pending_kind == "question": the {header: answer} dict the
#                 browser POSTed to /api/chat/answer, waiting to be handed to
#                 the parked generator when the resume request arrives
#   stop          asyncio.Event set by /api/chat/stop
#   lock          serialises streams; a second POST waits rather than
#                 interleaving two writers into the same msgs list
#   last_checkpoint_index  as_of_index of the last compaction checkpoint
#                 bridge.py has already flashed to the browser for — mirrors
#                 start_live_tui_'s self._last_checkpoint_index, same reason:
#                 seeded from whatever's already on disk at init() so a
#                 compaction from a *previous* session doesn't false-flash
#                 "compacted just now" on this session's first turn.
STATE = {
    "project_dir": None,
    "msgs": [],
    "gen": None,
    "pending": None,
    "pending_kind": None,
    "pending_tool": None,
    "answers": None,
    "stop": None,
    "lock": None,
    "model": None,
    "dangerous": set(),
    "trim_budget": None,
    "last_checkpoint_index": 0,
}


def init(project_dir, msgs, model, dangerous, trim_budget):
    existing_checkpoint = load_checkpoint(project_dir)
    STATE.update(
        project_dir=project_dir,
        msgs=msgs,
        gen=None,
        pending=None,
        pending_kind=None,
        pending_tool=None,
        answers=None,
        stop=asyncio.Event(),
        lock=asyncio.Lock(),
        model=model,
        dangerous=set(dangerous),
        trim_budget=trim_budget,
        last_checkpoint_index=(
            existing_checkpoint["as_of_index"] if existing_checkpoint is not None else 0
        ),
    )


def set_pending(kind, mutable, tool):
    STATE["pending"] = mutable
    STATE["pending_kind"] = kind
    STATE["pending_tool"] = tool


def clear_pending():
    STATE["pending"] = None
    STATE["pending_kind"] = None
    STATE["pending_tool"] = None
    STATE["answers"] = None


def resolve_pending(value):
    """Fill in the dict claude_loop is blocked on. Returns False if nothing is
    actually waiting — a stale Approve click after a reload, say, which must
    not be mistaken for a fresh turn.

    For an approval, `value` is a bool. For a question it is the
    {header: answer} dict ask_user_question_tool_ hands back to the model, so
    it must never be coerced to a bool — claude_loop only checks it for
    truthiness (falsy cancels the turn) and otherwise passes it straight
    through as the tool result.
    """
    pending, kind = STATE["pending"], STATE["pending_kind"]
    if pending is None:
        return False
    pending["approved" if kind == "approval" else "answered"] = value
    return True
