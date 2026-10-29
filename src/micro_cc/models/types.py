"""Provider-independent response contract: dict-shaped history, attribute-shaped content blocks."""

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Literal, TypedDict


@dataclass
class ContentBlock:
    type: str
    text: str = ""
    thinking: str = ""
    # Anthropic: thinking signature, checked on replay. Responses API: encrypted_content (opaque blob).
    signature: str = ""
    name: str = ""
    input: dict = field(default_factory=dict)
    # tool_use call id; Responses API reasoning item id, needed to replay it ahead of its function_call.
    id: str = ""
    # JSONDecodeError message when streamed tool args don't parse; input stays {}, id/name kept for an is_error result.
    parse_error: str = ""


class Usage(TypedDict, total=False):
    # Total input incl. cached tokens; output for this call.
    input: int
    output: int
    # Cache split for miss detection; absent when the provider doesn't report caching.
    cache_read: int
    cache_write: int


@dataclass
class Response:
    content: list = field(default_factory=list)
    usage: Usage = field(default_factory=dict)
    # Anthropic naming ("max_tokens", "refusal", ...); other adapters map onto it, "" if unknown.
    stop_reason: str = ""
    stop_details: object = None


class TextDelta(TypedDict):
    type: Literal["text_delta"]
    text: str


class ThinkingDelta(TypedDict):
    type: Literal["thinking_delta"]
    thinking: str


class ToolUseStart(TypedDict):
    type: Literal["tool_use_start"]
    name: str


class ResponseEvent(TypedDict):
    type: Literal["response"]
    response: Response
    usage: Usage


class StreamReset(TypedDict):
    """Stream died mid-turn and was re-sent: drop everything streamed before this."""
    type: Literal["stream_reset"]
    error: str
    attempt: int


StreamEvent = TextDelta | ThinkingDelta | ToolUseStart | ResponseEvent | StreamReset
ResponseStream = AsyncIterator[StreamEvent]
