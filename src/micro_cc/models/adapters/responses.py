"""OpenAI Responses output (direct API and LiteLLM)."""

import json

from micro_cc.models.types import ContentBlock, Response, ResponseStream


def _blocks_from_item(item) -> list:
    """One output item -> zero or more ContentBlocks, preserving item order
    (reasoning/function_call items can interleave within one turn on a
    multi-tool-call response, so callers append per-item rather than
    grouping by type)."""
    if item.type == "reasoning":
        summary = getattr(item, "summary", None) or []
        text = "\n".join(getattr(s, "text", "") for s in summary)
        # id/encrypted_content are kept even with no summary text — the
        # reasoning item itself is still needed on replay (see
        # models/openai.py's _msgs_to_responses).
        return [ContentBlock(
            type="thinking",
            thinking=text,
            id=item.id,
            signature=getattr(item, "encrypted_content", None) or "",
        )]
    if item.type == "message":
        return [
            ContentBlock(type="text", text=part.text)
            for part in item.content
            if getattr(part, "type", None) == "output_text"
        ]
    if item.type == "function_call":
        parse_error = ""
        try:
            args = json.loads(item.arguments)
        except (json.JSONDecodeError, TypeError) as e:
            args = {}
            parse_error = str(e)
        return [ContentBlock(
            type="tool_use", name=item.name, input=args, id=item.call_id, parse_error=parse_error,
        )]
    return []


def response(resp) -> Response:
    """Normalize a Responses API result, including non-stream usage."""
    blocks = []
    for item in resp.output:
        blocks.extend(_blocks_from_item(item))

    usage = {}
    u = getattr(resp, "usage", None)
    if u:
        usage["input"] = getattr(u, "input_tokens", 0)
        usage["output"] = getattr(u, "output_tokens", 0)
    return Response(content=blocks, usage=usage)


async def stream_response(resp_stream) -> ResponseStream:
    """Accumulate Responses API stream events, yield Anthropic-shaped
    events, return final Response with ContentBlocks.

    Final blocks are built from output_item.done items rather than the
    accumulated deltas: encrypted_content never streams as a delta (it
    lands whole on the done event, and output_item.added's copy can be
    truncated per OpenAI's own docs), and building per-item preserves true
    output order for a turn with more than one reasoning/function_call
    pair — matching response()'s non-stream handling of the same item
    stream via _blocks_from_item.
    """
    items = []
    usage = {}

    async for ev in resp_stream:
        et = ev.type
        if et == "response.output_text.delta":
            yield {"type": "text_delta", "text": ev.delta}
        elif et == "response.reasoning_summary_text.delta":
            yield {"type": "thinking_delta", "thinking": ev.delta}
        elif et == "response.output_item.done":
            items.append(ev.item)
        elif et == "response.completed":
            u = ev.response.usage
            if u:
                usage["input"] = getattr(u, "input_tokens", 0)
                usage["output"] = getattr(u, "output_tokens", 0)

    blocks = [b for item in items for b in _blocks_from_item(item)]
    yield {"type": "response", "response": Response(content=blocks, usage=usage), "usage": usage}
