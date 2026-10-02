"""Native-renderer sibling of etype_handler_.py — same ETYPES dispatch
table, same 10 event types, same state transitions. Nothing about WHEN
something gets stored/mounted/finalized changes here, only which line
touches a widget. etype_handler_.py itself is untouched and still
backs the old Textual start_live_.py path.

`container` (passed in from MicroTui.do_query) is a tui_native.stack_.
VStack, not a Textual widget — every `await container.mount(row)` became
`container.add(row)` (sync, no await), and every `app.query_one(...)`
became a plain attribute on MicroTui (app.prompt, app.set_focus(...)).
See start_live_tui_.py's translation table comment block for the full
touchpoint list this mirrors.
"""

import asyncio

from micro_cc.utils import theme_store_
from micro_cc.utils.msg_store_ import store_msgs
from micro_cc.utils.tokenization_simple import CACHE_TTL_S
from micro_cc.tui_native.message_row_ import MessageRow, StreamingRow


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


async def handle_tool_call(app, event, container, tool_call_rows, approval_rows):
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


# Reveal cadence for text/thinking deltas — same trick as webui/bridge.py's
# reveal(): pace the OUTPUT inline with a sleep between slices, rather than
# buffering and draining on a separate timer. Because this coroutine is what
# the `async for event in claude_loop(...)` loop in do_query awaits, pulling
# the next event off claude_loop is itself held back until the current
# delta finishes trickling out — the pacing throttles the source, so there's
# never a backlog to burst-flush later (see StreamingRow's docstring).
_FRAME = 0.012
_CHARS_PER_TICK = 6


async def _stream_delta(app, event, container, mode):
    if app._streaming is None or app._streaming._mode != mode:
        app._finalize_streaming()
        app._streaming = StreamingRow(mode=mode)
        container.add(app._streaming)
    text = event.get("content", "")
    row = app._streaming
    for i in range(0, len(text), _CHARS_PER_TICK):
        row.append(text[i:i + _CHARS_PER_TICK])
        app.request_render()
        await asyncio.sleep(_FRAME)


async def handle_text_delta(app, event, container, tool_call_rows, approval_rows):
    await _stream_delta(app, event, container, "text")


async def handle_thinking_delta(app, event, container, tool_call_rows, approval_rows):
    await _stream_delta(app, event, container, "thinking")


async def handle_turn_boundary(app, event, container, tool_call_rows, approval_rows):
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

    app.set_focus(app.prompt)
    app._pending_input = asyncio.Event()
    app._pending_result = None
    await app._pending_input.wait()

    approval["approved"] = app._pending_result
    if tool_id in approval_rows:
        arow = approval_rows.pop(tool_id)
        if not app._pending_result:
            new_msg = {"type": "error", "content": f" {name} cancelled"}
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


async def handle_question_asked(app, event, container, tool_call_rows, approval_rows):
    app._input_mode = "question_asked"
    app._alert_attention()

    answered = event.get("answered")
    inp = event.get("input", {})

    app._ask_questions = inp.get("questions", [])
    app._ask_stage = 0
    app._ask_answers = {}

    app._show_current_ask_question()

    app._pending_input = asyncio.Event()
    app._pending_result = None
    await app._pending_input.wait()

    answered["answered"] = app._pending_result
    app._pending_input = None
    # Reset _input_mode BEFORE _hide_ask_ui: the bars' repaint paths
    # (_static_hint_text / _update_status_bar) no-op while the mode is
    # "question_asked", so restoring them is only unblocked once the mode has
    # left that state. Doing it after would leave both bars blank for the rest
    # of the turn.
    app._input_mode = "query_active"
    app._hide_ask_ui()


async def handle_cache_invalidate(app, event, container, tool_call_rows, approval_rows):
    """Flash a short, honest reason — never a guess beyond what's actually
    observable (see detect_cache_miss's docstring): a model switch, an idle
    gap past Anthropic's TTL, or, absent either, a bare "cache miss" rather
    than a fabricated cause."""
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
    # TextLine's markup vocabulary is [red] and [#rrggbb] hex only — no
    # [yellow] — so this uses the theme's error token directly.
    app._flash_status(f"[{theme_store_.get('error')}]⚠ {label}: {tokens} tokens re-billed[/{theme_store_.get('error')}]", seconds=5)


ETYPES = {
    "tool_call": handle_tool_call,
    "tool_result": handle_tool_result,
    "final_text": handle_final_text,
    "done": handle_done,
    "error": handle_error,
    "text_delta": handle_text_delta,
    "thinking_delta": handle_thinking_delta,
    "approval_request": handle_approval_request,
    "question_asked": handle_question_asked,
    "turn_boundary": handle_turn_boundary,
    "cache_invalidate": handle_cache_invalidate,
}
