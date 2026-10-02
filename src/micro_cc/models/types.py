"""The application's response contract, independent of any provider SDK.

Requests/history use Anthropic-shaped dictionaries. Responses expose content
blocks by attribute; Anthropic SDK blocks already satisfy that interface and
are kept intact. Other protocols construct ContentBlock values here.
"""

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Literal, TypedDict


@dataclass
class ContentBlock:
    type: str
    text: str = ""
    thinking: str = ""
    # Anthropic: cryptographic signature on a "thinking" block, validated on
    # replay. OpenAI Responses API: reused to carry a reasoning item's
    # encrypted_content — same role (an opaque blob the provider must get
    # back verbatim to trust/reuse the reasoning), different provider.
    signature: str = ""
    name: str = ""
    input: dict = field(default_factory=dict)
    # tool_use: the call id. OpenAI Responses API "thinking" blocks: reused
    # to carry the reasoning item's own id, needed to replay it ahead of the
    # function_call items it preceded (see adapters/responses.py, models/openai.py).
    id: str = ""
    # Set (to the JSONDecodeError message) when a tool_use block's streamed
    # arguments didn't parse as JSON. `input` is left {} rather than the
    # model's raw, invented-looking partial text — but the block still
    # carries id/name so claude_loop_ can pair it with an is_error
    # tool_result instead of dropping it and violating the API's "one
    # tool_result per tool_use" contract.
    parse_error: str = ""


class Usage(TypedDict, total=False):
    # Full input context, including cached tokens; output generated this call.
    input: int
    output: int
    # Split of `input` for cache-miss detection (utils/tokenization_simple.py's
    # detect_cache_miss). Absent, not zero, when a provider doesn't report
    # prompt-cache activity at all — the two adapters that do (anthropic.py)
    # always set both, even when a value is legitimately 0.
    cache_read: int
    cache_write: int


@dataclass
class Response:
    content: list = field(default_factory=list)
    usage: Usage = field(default_factory=dict)
    # Raw values off the API response — "" / None for providers that don't
    # have this concept (only the Anthropic-family adapters populate them).
    # "refusal" is the one claude_loop_ checks for: a terminal turn with no
    # content blocks at all (safety classifiers, not a normal end_turn) that
    # would otherwise be indistinguishable from an ordinary empty completion.
    stop_reason: str = ""
    stop_details: object = None


class TextDelta(TypedDict):
    type: Literal["text_delta"]
    text: str


class ThinkingDelta(TypedDict):
    type: Literal["thinking_delta"]
    thinking: str


class ResponseEvent(TypedDict):
    type: Literal["response"]
    response: Response
    usage: Usage


StreamEvent = TextDelta | ThinkingDelta | ResponseEvent
ResponseStream = AsyncIterator[StreamEvent]
