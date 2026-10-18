"""Live GUI state: one project, one browser, one process.

claude_loop suspends on approval/question, so the suspended generator is the pending state; this works because server and loop share a process.
"""

import asyncio
import itertools
import secrets

from micro_cc.utils.msg_store_ import load_checkpoint

# One project per process, so a plain module-level dict. Fields:
#   project_dir   str
#   msgs          live transcript handed to claude_loop
#   gen           suspended claude_loop generator, or None
#   pending       mutable dict claude_loop blocks on ({"approved"|"answered": None}), or None
#   pending_kind  "approval" | "question"
#   pending_tool  {"id", "name", "input"} of the waiting call, for re-render
#   answers       {header: answer} from /api/chat/answer, handed to the parked generator on resume
#   denied        True once /api/chat/deny ran; the resume hands False to the parked approval
#   stop          asyncio.Event set by /api/chat/stop
#   lock          serialises streams (one writer per msgs list)
#   last_checkpoint_index  compaction index already flashed; seeded in init() so an old compaction doesn't re-flash
#   incoming      server-initiated turns waiting to be claimed (see /api/incoming)
STATE = {
    "project_dir": None,
    "msgs": [],
    "gen": None,
    "pending": None,
    "pending_kind": None,
    "pending_tool": None,
    "answers": None,
    "denied": False,
    "stop": None,
    "lock": None,
    "model": None,
    "trim_budget": None,
    "last_checkpoint_index": 0,
    "incoming": [],
    "partial": [],   # streamed assistant text not yet in msgs
    "token": None,   # one-time bootstrap secret in the /gui link
    "cookie": None,  # session secret; None disables auth (tests)
    "owner": None,   # id of the one tab bound to this session
}


def init(project_dir, msgs, model, trim_budget, token=None):
    existing_checkpoint = load_checkpoint(project_dir)
    STATE.update(
        project_dir=project_dir,
        msgs=msgs,
        gen=None,
        pending=None,
        pending_kind=None,
        pending_tool=None,
        answers=None,
        denied=False,
        stop=asyncio.Event(),
        lock=asyncio.Lock(),
        model=model,
        trim_budget=trim_budget,
        incoming=[],
        partial=[],
        token=token,
        cookie=secrets.token_urlsafe(24) if token else None,
        owner=None,
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
    STATE["denied"] = False


def resolve_pending(value):
    """Fill the dict claude_loop is blocked on; False if nothing waits (e.g. a stale click).

    Question answers are {header: answer} dicts and must never be coerced to bool.
    """
    pending, kind = STATE["pending"], STATE["pending_kind"]
    if pending is None:
        return False
    pending["approved" if kind == "approval" else "answered"] = value
    return True


_incoming_seq = itertools.count(1)


def push_incoming(content: str) -> None:
    STATE["incoming"].append({"id": next(_incoming_seq), "content": content})


def take_incoming(incoming_id) -> str | None:
    """Pop the item with this id; None if another claimant already took it."""
    for i, item in enumerate(STATE["incoming"]):
        if item["id"] == incoming_id:
            return STATE["incoming"].pop(i)["content"]
    return None


def drain_incoming() -> list:
    items, STATE["incoming"] = STATE["incoming"], []
    return [i["content"] for i in items]
