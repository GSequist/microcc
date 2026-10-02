"""Translate claude_loop's event dicts into a Vercel AI SDK v6 UI message stream.

claude_loop yields bare `text_delta`/`thinking_delta` with no block framing —
the TUI doesn't need any, it just appends to a widget. The SDK does: every run
of text has to sit inside a text-start/text-end pair with a stable id, and
tool calls have to sit inside a step. So this module owns that bookkeeping,
via close_text/close_thinking/open_step helpers.

Suspension is the interesting part. On `approval_request` we hand the mutable
dict to session.set_pending, close the stream cleanly (finish + [DONE]) and
`return` — crucially WITHOUT closing the underlying generator, which stays
parked on its `yield` until the next request resumes it.
"""

import asyncio
import json
import uuid

from micro_cc.cache import state_store
from micro_cc.utils.msg_store_ import load_checkpoint, store_msgs
from micro_cc.webui import session

SSE_DONE = "data: [DONE]\n\n"

# Events after which the transcript is worth flushing to disk — mirrors the
# set start_headless_ persists on.
_PERSIST_ON = {"tool_call", "tool_result", "final_text", "done", "error"}

# Reveal cadence for text/thinking deltas — same idea as StreamingRow in
# screens/window_overlay_.py (fixed-size slice per tick, not a wall-clock
# catch-up that bursts when a tick runs late), just paced inline instead of
# via a separate timer: asyncio.sleep() yields to the event loop every tick,
# so a concurrent /api/chat/stop POST still gets scheduled and server.py's
# stop check (run between every chunk this yields) still fires promptly.
_FRAME = 0.02
_CHARS_PER_TICK = 2


def sse(chunk: dict) -> str:
    return f"data: {json.dumps(chunk)}\n\n"


def _compaction_flash(project_dir):
    """Webui counterpart to the TUI's _update_status_bar compaction-flash
    check — same poll-on-natural-stopping-points cadence (called from the
    same _PERSIST_ON set below), same disk-backed checkpoint, same
    stale-index seeding fix (see session.init). Returns an SSE chunk for a
    fresh compaction, else None. A stable id ("status") lets AI SDK update
    the one data-status part in place rather than piling up duplicates;
    `at` is the checkpoint index the frontend keys its own re-arm off."""
    checkpoint = load_checkpoint(project_dir)
    if checkpoint is None or checkpoint["as_of_index"] == session.STATE["last_checkpoint_index"]:
        return None
    session.STATE["last_checkpoint_index"] = checkpoint["as_of_index"]
    return sse({
        "type": "data-status",
        "id": "status",
        "data": {"text": "✂ compacted past conversation just now", "at": checkpoint["as_of_index"]},
    })


async def stream(gen, msg_id, project_dir):
    """Drive `gen` (a fresh or resumed claude_loop) and yield SSE strings.

    Returns normally either when the loop finishes or when it suspends on an
    approval; `session.STATE["pending"]` tells the caller which happened.
    """
    in_text = in_thinking = step_open = False
    text_id = thinking_id = None

    def close_text():
        nonlocal in_text, text_id
        if in_text:
            tid, in_text, text_id = text_id, False, None
            return sse({"type": "text-end", "id": tid})

    def close_thinking():
        nonlocal in_thinking, thinking_id
        if in_thinking:
            tid, in_thinking, thinking_id = thinking_id, False, None
            return sse({"type": "reasoning-end", "id": tid})

    def open_step():
        nonlocal step_open
        if not step_open:
            step_open = True
            return sse({"type": "start-step"})

    def close_step():
        nonlocal step_open
        if step_open:
            step_open = False
            return sse({"type": "finish-step"})

    def close_all():
        return [c for c in (close_thinking(), close_text(), close_step()) if c]

    async def reveal(chunk_type, block_id, text):
        """Yield `text` as small fixed-size SSE deltas, paced with a sleep
        between each — the smoothing step. `block_id` is captured by value at
        call time, so a delta always carries the id that was live when it
        started even if the block gets closed for any reason before this
        finishes draining."""
        for i in range(0, len(text), _CHARS_PER_TICK):
            yield sse({"type": chunk_type, "id": block_id, "delta": text[i:i + _CHARS_PER_TICK]})
            await asyncio.sleep(_FRAME)

    yield sse({"type": "start", "messageId": msg_id})

    async for event in gen:
        etype = event.get("type")

        if etype in _PERSIST_ON:
            store_msgs(project_dir, session.STATE["msgs"])
            flash = _compaction_flash(project_dir)
            if flash:
                yield flash

        if etype == "thinking_delta":
            for c in (close_text(), open_step()):
                if c:
                    yield c
            if not in_thinking:
                in_thinking = True
                thinking_id = f"reasoning-{uuid.uuid4().hex[:8]}"
                yield sse({"type": "reasoning-start", "id": thinking_id})
            async for c in reveal("reasoning-delta", thinking_id, event["content"]):
                yield c

        elif etype == "text_delta":
            for c in (close_thinking(), open_step()):
                if c:
                    yield c
            if not in_text:
                in_text = True
                text_id = f"text-{uuid.uuid4().hex[:8]}"
                yield sse({"type": "text-start", "id": text_id})
            async for c in reveal("text-delta", text_id, event["content"]):
                yield c

        elif etype == "tool_call":
            for c in (close_thinking(), close_text(), open_step()):
                if c:
                    yield c
            yield sse({
                "type": "tool-input-available",
                "toolCallId": event["id"],
                "toolName": event["name"],
                "input": event.get("input") or {},
                "dynamic": True,
            })

        elif etype == "tool_result":
            yield sse({
                "type": "tool-output-available",
                "toolCallId": event["id"],
                "output": event.get("output", ""),
            })
            # The todo store lives in state, not in the tool's return string —
            # re-read it so the tracker reflects the write that just happened.
            if event.get("name") == "todo_tool_":
                todos = state_store.get_todos(project_dir)
                if todos:
                    yield sse({"type": "data-todos", "id": "todos", "data": {"todos": todos}})

        elif etype in ("approval_request", "question_asked"):
            kind = "approval" if etype == "approval_request" else "question"
            mutable = event["approval"] if kind == "approval" else event["answered"]
            tool_input = event.get("input") or {}
            session.set_pending(
                kind,
                mutable,
                {"id": event["id"], "name": event["name"], "input": tool_input},
            )
            for c in close_all():
                yield c

            if kind == "question":
                # ask_user_question_tool_ carries a LIST of questions, each
                # freeform / single-choice / multi-choice, shown one at a time
                # — none of which survives being squashed into Approve/Deny.
                # The questions ride out as a data part for the modal to walk;
                # the approval-request that follows exists purely to arm the
                # SDK's resume (sendAutomaticallyWhen), which is what re-POSTs
                # /api/chat once the answers are in.
                yield sse({
                    "type": "data-question",
                    "id": f"question-{event['id']}",
                    "data": {
                        "toolCallId": event["id"],
                        "approvalId": f"question-{event['id']}",
                        "questions": tool_input.get("questions", []),
                    },
                })

            yield sse({
                "type": "tool-approval-request",
                "approvalId": f"{kind}-{event['id']}",
                "toolCallId": event["id"],
            })
            yield sse({"type": "finish", "finishReason": "stop"})
            yield SSE_DONE
            # Deliberately no aclose() — the generator stays parked on its
            # yield, and STATE["gen"] still references it. The next POST
            # resolves the dict and iterates it onwards from here.
            return

        elif etype == "cancelled":
            tool = session.STATE.get("pending_tool") or {}
            if tool.get("id"):
                yield sse({
                    "type": "tool-output-available",
                    "toolCallId": tool["id"],
                    "output": "User denied this action.",
                })
            session.clear_pending()
            for c in close_all():
                yield c

        elif etype == "final_text":
            for c in (close_thinking(), close_text()):
                if c:
                    yield c

        elif etype == "error":
            for c in close_all():
                yield c
            yield sse({"type": "error", "errorText": event.get("message", "unknown error")})

        elif etype == "done":
            for c in close_all():
                yield c

    for c in close_all():
        yield c
    store_msgs(project_dir, session.STATE["msgs"])
    yield sse({"type": "finish", "finishReason": "stop"})
    yield SSE_DONE
