"""OpenAI Chat Completions output, including LiteLLM/Ollama/OpenRouter reasoning.

The wire protocol is shared; only reasoning extraction varies by provider.
These functions consume streams in the caller's task without prefetch/retry.
"""

import json
from typing import Literal

from micro_cc.models.types import ContentBlock, Response, ResponseStream, Usage

Reasoning = Literal["none", "litellm", "ollama", "openrouter"]


def _reasoning_text(obj, reasoning: Reasoning) -> str:
    if reasoning == "litellm":
        return getattr(obj, "reasoning_content", None) or ""
    if reasoning in ("ollama", "openrouter"):
        # Same plain "reasoning" string field on both.
        text = getattr(obj, "reasoning", None)
        if text:
            return text
        try:
            return obj.model_dump().get("reasoning") or ""
        except Exception:
            return ""
    return ""


def _usage(raw) -> Usage:
    u = getattr(raw, "usage", None)
    if not u:
        return {}
    return {
        "input": getattr(u, "prompt_tokens", 0),
        "output": getattr(u, "completion_tokens", 0),
    }


def response(raw, *, reasoning: Reasoning = "none") -> Response:
    """Normalize a completion, preserving LiteLLM's signed thinking block."""
    msg = raw.choices[0].message
    blocks = []
    thinking_blocks = getattr(msg, "thinking_blocks", None) if reasoning == "litellm" else None
    if thinking_blocks:
        tb = thinking_blocks[0]
        blocks.append(ContentBlock(
            type="thinking",
            thinking=tb.get("thinking", "") if isinstance(tb, dict) else getattr(tb, "thinking", ""),
            signature=tb.get("signature", "") if isinstance(tb, dict) else getattr(tb, "signature", ""),
        ))
    else:
        text = _reasoning_text(msg, reasoning)
        if text:
            blocks.append(ContentBlock(type="thinking", thinking=text))

    if msg.content:
        blocks.append(ContentBlock(type="text", text=msg.content))
    for tc in msg.tool_calls or []:
        parse_error = ""
        try:
            args = json.loads(tc.function.arguments)
        except (json.JSONDecodeError, TypeError) as e:
            args = {}
            parse_error = str(e)
        blocks.append(ContentBlock(
            type="tool_use", name=tc.function.name, input=args, id=tc.id, parse_error=parse_error,
        ))
    return Response(content=blocks, usage=_usage(raw))


async def stream_response(raw_stream, *, reasoning: Reasoning = "none") -> ResponseStream:
    """Yield deltas as received, then one response with assembled tool calls."""
    text = ""
    thinking = ""
    tool_calls = {}  # index -> {id, name, args}; insertion order is call order
    usage = {}

    async for chunk in raw_stream:
        # include_usage may produce a choices=[] trailer on any chat backend.
        usage.update(_usage(chunk))
        if not chunk.choices:
            continue
        delta = chunk.choices[0].delta
        if delta.content:
            text += delta.content
            yield {"type": "text_delta", "text": delta.content}
        rc = _reasoning_text(delta, reasoning)
        if rc:
            thinking += rc
            yield {"type": "thinking_delta", "thinking": rc}
        for tc in delta.tool_calls or []:
            if tc.index not in tool_calls:
                tool_calls[tc.index] = {
                    "id": tc.id or "",
                    "name": tc.function.name if tc.function else "",
                    "args": "",
                }
            if tc.function and tc.function.arguments:
                tool_calls[tc.index]["args"] += tc.function.arguments

    blocks = []
    if thinking:
        blocks.append(ContentBlock(type="thinking", thinking=thinking))
    if text:
        blocks.append(ContentBlock(type="text", text=text))
    for tc in tool_calls.values():
        # Malformed streamed arguments never execute as a partial call with
        # invented input — but parse failure is flagged on the block
        # (parse_error) rather than raised: raising here used to propagate
        # out of this whole generator and kill the entire turn (claude_loop_'s
        # outer except-and-break) over one bad tool call. claude_loop_ instead
        # hands the model an is_error tool_result and continues, same
        # treatment as a hallucinated tool name.
        parse_error = ""
        args = {}
        if tc["args"]:
            try:
                args = json.loads(tc["args"])
            except json.JSONDecodeError as e:
                parse_error = str(e)
        blocks.append(ContentBlock(
            type="tool_use", name=tc["name"], input=args, id=tc["id"], parse_error=parse_error,
        ))
    result = Response(content=blocks, usage=usage)
    yield {"type": "response", "response": result, "usage": usage}
