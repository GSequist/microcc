"""Native-renderer event type handlers; same event types and state transitions."""

import asyncio

from micro_cc.utils import theme_store_
from micro_cc.utils.msg_store_ import store_msgs
from micro_cc.utils.tokenization_simple import CACHE_TTL_S
from micro_cc.tui_native.message_row_ import MessageRow, StreamingRow
from micro_cc.tui_native import question_ui_
from micro_cc.tui_native import approval_ui_


async def handle_done(app, event, container, tool_call_rows, approval_rows):
    store_msgs(app._project_dir, app._loop_msgs)
    app._finalize_streaming()
    app._scroll_to_bottom()


async def handle_final_text(app, event, container, tool_call_rows, approval_rows):
    store_msgs(app._project_dir, app._loop_msgs)
    app._finalize_streaming()
    app._scroll_to_bottom()
    app._refresh_status()


async def handle_error(app, event, container, tool_call_rows, approval_rows):
    store_msgs(app._project_dir, app._loop_msgs)
    app._finalize_streaming()
    err = {"type": "error", "content": event.get("message", "Unknown error")}
    app._msg_rows.append(err)
    container.add(MessageRow(err))
    app._scroll_to_bottom()


async def handle_mcp_auth(app, event, container, tool_call_rows, approval_rows):
    """MCP OAuth login prompt: show the auth URL inline (no browser on WSL/SSH, or as a fallback)."""
    row = {"type": "mcp_auth", "content": event.get("message", "")}
    app._msg_rows.append(row)
    container.add(MessageRow(row))
    app._scroll_to_bottom()


async def handle_turn_reset(app, event, container, tool_call_rows, approval_rows):
    """Discarded partial turn: drop its streamed rows (trailing text/thinking) so the replay isn't doubled."""
    if app._streaming is not None:
        container.remove(app._streaming)
        app._streaming = None
    while app._msg_rows and app._msg_rows[-1].get("type") in ("text", "thinking"):
        msg = app._msg_rows.pop()
        rows = [r for r, m in app._draining if m is msg]
        rows += [e.component for e in container.entries if getattr(e.component, "_msg", None) is msg]
        app._draining = [(r, m) for r, m in app._draining if m is not msg]
        for row in rows:
            container.remove(row)
    err = {"type": "error", "content": event.get("message", "turn reset")}
    app._msg_rows.append(err)
    container.add(MessageRow(err))
    app._scroll_to_bottom()


async def handle_tool_use_start(app, event, container, tool_call_rows, approval_rows):
    app._pending_tool = event.get("name") or None
    app._tick_working()


async def handle_tool_call(app, event, container, tool_call_rows, approval_rows):
    app._pending_tool = None
    store_msgs(app._project_dir, app._loop_msgs)
    app._finalize_streaming()
    msg = {
        "type": "tool_call",
        "name": event.get("name", "?"),
        "id": event.get("id"),
        "input": event.get("input", {}),
        "result": None,
        "expanded": False,
    }
    row = MessageRow(msg)
    app._msg_rows.append(msg)
    tool_call_rows[event.get("id")] = (row, msg)
    container.add(row)
    app._scroll_to_bottom()


async def handle_tool_result(app, event, container, tool_call_rows, approval_rows):
    store_msgs(app._project_dir, app._loop_msgs)
    tool_id = event.get("id")
    if tool_id in tool_call_rows:
        row, msg = tool_call_rows[tool_id]
        msg["result"] = event.get("output", "")
        result_contains_image = event.get("image_data", "")
        if result_contains_image:
            msg["result"] = {"type": "image", "data": event["image_data"]}
        row.update_msg(msg)
        app._scroll_to_bottom()


# Buffer only; the render loop paces the reveal (app._tick_stream) so the stream is never throttled.
async def _stream_delta(app, event, container, mode):
    if app._streaming is None or app._streaming._mode != mode:
        app._finalize_streaming()
        app._streaming = StreamingRow(mode=mode)
        container.add(app._streaming)
    app._streaming.append(event.get("content", ""))


async def handle_text_delta(app, event, container, tool_call_rows, approval_rows):
    await _stream_delta(app, event, container, "text")


async def handle_thinking_delta(app, event, container, tool_call_rows, approval_rows):
    await _stream_delta(app, event, container, "thinking")


async def handle_turn_boundary(app, event, container, tool_call_rows, approval_rows):
    app._pending_tool = None
    app._refresh_status()

    if not app._queue:
        return
    while app._queue:
        next_q, next_row, next_msg, _next_image = app._queue.popleft()
        container.remove(next_row)
        if next_msg in app._msg_rows:
            app._msg_rows.remove(next_msg)
        await app._mount_row({"type": "user", "content": next_q})
        app._loop_msgs.append({"role": "user", "content": next_q})
    store_msgs(app._project_dir, app._loop_msgs)


async def handle_approval_request(app, event, container, tool_call_rows, approval_rows):
    app._input_mode = "awaiting_approval"
    app._alert_attention()

    approval = event.get("approval")
    tool_id = event.get("id")
    name = event.get("name", "")
    inp = event.get("input", {})

    msg = {"type": "approval", "id": tool_id, "name": name, "input": inp, "expanded": False}
    row = MessageRow(msg)
    approval_rows[tool_id] = row
    app._msg_rows.append(msg)
    container.add(row)

    app._scroll_to_bottom()

    app._pending_input = asyncio.Event()
    app._pending_result = None
    approval_ui_.show(app, name, inp)
    await app._pending_input.wait()

    # claude_loop reads this after the yield resumes: falsy -> denial tool_result, turn continues.
    approval["approved"] = app._pending_result
    if app._pending_result == "always":
        app._ungate_tool(name)

    if tool_id in approval_rows:
        arow = approval_rows.pop(tool_id)
        if not app._pending_result:
            new_msg = {"type": "error", "content": f" {name} declined"}
            arow.update_msg(new_msg)
            for i, m in enumerate(app._msg_rows):
                if m.get("type") == "approval" and m.get("id") == tool_id:
                    app._msg_rows[i] = new_msg
                    break
        else:
            container.remove(arow)
            app._msg_rows = [m for m in app._msg_rows if not (m.get("type") == "approval" and m.get("id") == tool_id)]
    app._pending_input = None
    app._input_mode = "query_active"
    approval_ui_.hide(app)


async def handle_question_asked(app, event, container, tool_call_rows, approval_rows):
    app._input_mode = "question_asked"
    app._alert_attention()

    answered = event.get("answered")
    inp = event.get("input", {})

    app._pending_input = asyncio.Event()
    app._pending_result = None
    question_ui_.start(app, inp.get("questions", []))
    await app._pending_input.wait()

    answered["answered"] = app._pending_result
    app._pending_input = None
    # Reset _input_mode BEFORE _hide_ask_ui to unblock bar repainting.
    app._input_mode = "query_active"
    question_ui_.hide(app)


async def handle_cache_invalidate(app, event, container, tool_call_rows, approval_rows):
    """Flash cache miss reason: model switch, idle gap, or bare cache miss."""
    if event.get("model_changed"):
        reason = "model switch"
    elif event.get("changed_segments"):
        reason = " + ".join(event["changed_segments"]) + " changed"
    elif event.get("idle_s", 0) >= CACHE_TTL_S:
        reason = f"{round(event['idle_s'] / 60)}m idle"
    else:
        reason = None

    tokens = f"{event.get('missed_tokens', 0):,}"
    label = f"cache miss ({reason})" if reason else "cache miss"
    # TextLine markup only supports [red] and [#rrggbb], so use theme's error token.
    app._flash_status(f"[{theme_store_.get('error')}]⚠ {label}: {tokens} tokens re-billed[/{theme_store_.get('error')}]", seconds=5)


ETYPES = {
    "tool_call": handle_tool_call,
    "tool_result": handle_tool_result,
    "final_text": handle_final_text,
    "done": handle_done,
    "error": handle_error,
    "turn_reset": handle_turn_reset,
    "mcp_auth": handle_mcp_auth,
    "text_delta": handle_text_delta,
    "tool_use_start": handle_tool_use_start,
    "thinking_delta": handle_thinking_delta,
    "approval_request": handle_approval_request,
    "question_asked": handle_question_asked,
    "turn_boundary": handle_turn_boundary,
    "cache_invalidate": handle_cache_invalidate,
}
