"""Anthropic Messages output (direct API and Foundry)."""

import json

from micro_cc.models.types import ContentBlock, Response, ResponseStream


def response(raw) -> Response:
    """Normalize usage while retaining SDK content blocks and their extra fields.

    Content blocks already implement the application's attribute interface;
    keeping them also preserves signed/redacted thinking without a lossy copy.
    """
    usage = {}
    u = getattr(raw, "usage", None)
    if u:
        usage = {
            "input": (getattr(u, "input_tokens", 0)
                      + getattr(u, "cache_read_input_tokens", 0)
                      + getattr(u, "cache_creation_input_tokens", 0)),
            "output": getattr(u, "output_tokens", 0),
            "cache_read": getattr(u, "cache_read_input_tokens", 0),
            "cache_write": getattr(u, "cache_creation_input_tokens", 0),
        }
    return Response(
        content=raw.content,
        usage=usage,
        stop_reason=getattr(raw, "stop_reason", "") or "",
        stop_details=getattr(raw, "stop_details", None),
    )


async def stream_response(raw_stream, unstealth=None) -> ResponseStream:
    """Iterate Anthropic SDK stream, yield standardized deltas + final Response.

    unstealth: optional str->str applied to each tool_use block's name —
    supplied by the direct provider for OAuth tool-name mapping. Foundry
    and API-key calls leave it unset."""
    text = ""
    thinking = ""
    thinking_signature = ""
    tool_blocks = []
    current_tool = None
    usage = {}
    stop_reason = ""
    stop_details = None

    async for event in raw_stream:
        if event.type == "message_start":
            u = getattr(event.message, "usage", None)
            if u:
                # input_tokens alone is only the NEW (uncached) tokens for this
                # turn — with the rolling cache breakpoint above, most of the
                # transcript is cache_read/cache_creation instead, so input_tokens
                # drops to near-zero after turn 1. The true context size sent to
                # the API is the sum of all three.
                usage["input"] = (
                    getattr(u, "input_tokens", 0)
                    + getattr(u, "cache_read_input_tokens", 0)
                    + getattr(u, "cache_creation_input_tokens", 0)
                )
                usage["cache_read"] = getattr(u, "cache_read_input_tokens", 0)
                usage["cache_write"] = getattr(u, "cache_creation_input_tokens", 0)

        elif event.type == "content_block_start":
            if event.content_block.type == "tool_use":
                current_tool = {"id": event.content_block.id, "name": event.content_block.name, "input_json": ""}

        elif event.type == "content_block_delta":
            if event.delta.type == "text_delta":
                text += event.delta.text
                yield {"type": "text_delta", "text": event.delta.text}
            elif event.delta.type == "thinking_delta":
                thinking += event.delta.thinking
                yield {"type": "thinking_delta", "thinking": event.delta.thinking}
            elif event.delta.type == "signature_delta":
                thinking_signature += event.delta.signature or ""
            elif event.delta.type == "input_json_delta":
                if current_tool:
                    current_tool["input_json"] += event.delta.partial_json

        elif event.type == "content_block_stop":
            if current_tool:
                parse_error = ""
                try:
                    args = json.loads(current_tool["input_json"]) if current_tool["input_json"] else {}
                except json.JSONDecodeError as e:
                    # Never invent arguments for a tool call whose streamed
                    # JSON didn't parse — but don't let this raise either:
                    # that used to propagate out of the whole generator and
                    # take down the entire turn (claude_loop_'s outer
                    # except-and-break) over one bad tool call. Flagging it
                    # on the block instead lets claude_loop_ hand the model
                    # an is_error tool_result and continue, same treatment
                    # as a hallucinated tool name.
                    args = {}
                    parse_error = str(e)
                tool_blocks.append(current_tool | {"input": args, "parse_error": parse_error})
                current_tool = None

        elif event.type == "message_delta":
            u = getattr(event, "usage", None)
            if u:
                usage["output"] = getattr(u, "output_tokens", 0)
            stop_reason = getattr(event.delta, "stop_reason", None) or stop_reason
            stop_details = getattr(event.delta, "stop_details", None) or stop_details

    blocks = []
    if thinking:
        blocks.append(ContentBlock(type="thinking", thinking=thinking, signature=thinking_signature))
    if text:
        blocks.append(ContentBlock(type="text", text=text))
    for tb in tool_blocks:
        name = unstealth(tb["name"]) if unstealth else tb["name"]
        blocks.append(ContentBlock(
            type="tool_use", name=name, input=tb["input"], id=tb["id"],
            parse_error=tb.get("parse_error", ""),
        ))

    yield {
        "type": "response",
        "response": Response(
            content=blocks, usage=usage,
            stop_reason=stop_reason, stop_details=stop_details,
        ),
        "usage": usage,
    }
