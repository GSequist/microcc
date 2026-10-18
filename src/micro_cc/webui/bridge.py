"""Translate claude_loop event dicts into a Vercel AI SDK v6 UI message stream.

Owns the text/reasoning/step block framing the SDK requires.
"""

import asyncio
import json
import uuid

from micro_cc.cache import state_store
from micro_cc.utils.msg_store_ import load_checkpoint, store_msgs
from micro_cc.utils.tool_safety_ import explain_gated, gate_risk
from micro_cc.webui import live, session

SSE_DONE = "data: [DONE]\n\n"

# Events that flush the transcript to disk; same set start_headless_ persists on.
_PERSIST_ON = {"tool_call", "tool_result", "final_text", "done", "error"}

# Reveal in fixed-size slices, paced with sleep so a concurrent /api/chat/stop still gets scheduled.
_FRAME = 0.02
_CHARS_PER_TICK = 2


def sse(chunk: dict) -> str:
    return f"data: {json.dumps(chunk)}\n\n"


def _compaction_flash(project_dir):
    """SSE chunk for a compaction newer than the last one flashed, else None."""
    checkpoint = load_checkpoint(project_dir)
    if checkpoint is None or checkpoint["as_of_index"] == session.STATE["last_checkpoint_index"]:
        return None
    session.STATE["last_checkpoint_index"] = checkpoint["as_of_index"]
    # Stable id: the AI SDK updates this part in place instead of stacking duplicates.
    return sse({
        "type": "data-status",
        "id": "status",
        "data": {"text": "✂ compacted past conversation just now", "at": checkpoint["as_of_index"]},
    })


async def stream(gen, msg_id, project_dir):
    """Drive `gen` and yield SSE strings; returns on finish or suspension (session.STATE["pending"] says which)."""
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
        """Yield `text` as small SSE deltas, sleeping between each (the smoothing step)."""
        for i in range(0, len(text), _CHARS_PER_TICK):
            yield sse({"type": chunk_type, "id": block_id, "delta": text[i:i + _CHARS_PER_TICK]})
            await asyncio.sleep(_FRAME)

    last_live = None

    def live_parts():
        """data- parts for the status line above the input; only when something changed."""
        nonlocal last_live
        subs, procs = live.subagents(project_dir), live.bgprocs(project_dir)
        key = live.change_key(subs, procs)
        if key == last_live:
            return []
        last_live = key
        return [
            sse({"type": "data-subagents", "id": "subagents", "data": {"subagents": subs}}),
            sse({"type": "data-bgprocs", "id": "bgprocs", "data": {"processes": procs}}),
        ]

    yield sse({"type": "start", "messageId": msg_id})
    for c in live_parts():
        yield c

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
            session.STATE["partial"].append(event["content"])
            if not in_text:
                in_text = True
                text_id = f"text-{uuid.uuid4().hex[:8]}"
                yield sse({"type": "text-start", "id": text_id})
            async for c in reveal("text-delta", text_id, event["content"]):
                yield c

        elif etype == "tool_call":
            session.STATE["partial"] = []  # now in msgs
            for c in (close_thinking(), close_text(), open_step()):
                if c:
                    yield c
            tool_input = event.get("input") or {}
            risk, reason = gate_risk(event["name"], tool_input, project_dir)
            yield sse({
                "type": "tool-input-available",
                "toolCallId": event["id"],
                "toolName": event["name"],
                "input": tool_input,
                "dynamic": True,
                # Read by the approval gate (part.toolMetadata); same text/risk as the TUI.
                "toolMetadata": {
                    "explanation": explain_gated(event["name"], tool_input),
                    "risk": risk,
                    "reason": reason,
                },
            })

        elif etype == "tool_result":
            yield sse({
                "type": "tool-output-available",
                "toolCallId": event["id"],
                "output": event.get("output", ""),
            })
            for c in live_parts():
                yield c
            # Todos live in state, not the tool's return value; re-read so the tracker sees the write.
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
                # Questions ride as a data part; the approval-request below only arms the SDK resume.
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
            # No aclose(): the generator stays parked; the next POST resumes it from here.
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
            session.STATE["partial"] = []  # now in msgs
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
    for c in live_parts():
        yield c
    store_msgs(project_dir, session.STATE["msgs"])
    yield sse({"type": "finish", "finishReason": "stop"})
    yield SSE_DONE
