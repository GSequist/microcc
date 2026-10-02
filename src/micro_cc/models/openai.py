"""Direct OpenAI model caller.

Uses shared Chat Completions/Responses output adapters. Request conversion,
OpenAI explicit prompt caching, credentials and retries remain provider-local.

Env vars:
    OPENAI_API_KEY:  API key
    OPENAI_BASE_URL: Optional override (self-hosted/compatible gateway) —
                      unset uses the SDK's default api.openai.com
"""

from typing import List, Dict, Any, Optional, Union
from openai import AsyncOpenAI
from dotenv import load_dotenv
import asyncio
import json
import os

from micro_cc.models.adapters import chat, responses
from micro_cc.models.priming import prime_stream, first_event_timeout
from micro_cc.models.schema import tools_to_chat, tools_to_responses

from micro_cc.models.registry import (
    DEFAULT_MODEL,
    resolve,
    supports_thinking,
    uses_responses_api,
    max_output_for,
)

load_dotenv(os.path.expanduser("~/.micro-cc/.env"))
load_dotenv()

# Seconds to wait for a streaming call's first event before treating a
# connected-but-silent endpoint as a failed attempt — see priming.prime_stream.
FIRST_EVENT_TIMEOUT = 120
# 60s was too tight for proxies: a slow first event is retried by re-sending the
# whole prompt. Override for all providers with MICROCC_FIRST_EVENT_TIMEOUT.


# ---------------------------------------------------------------------------
# Format converters — chat.completions path
# ---------------------------------------------------------------------------


def _msgs_to_openai(anthropic_msgs):
    """Convert Anthropic-format message list to OpenAI chat format.

    Handles: system, user (string/multimodal/tool_result),
    assistant (string/tool_use blocks). Images converted to OpenAI
    image_url format. Thinking blocks are dropped on this path — OpenAI's
    real chat-completions API doesn't return or accept reasoning traces;
    only the Responses API (see _msgs_to_responses below) surfaces those.
    """
    out = []
    for msg in anthropic_msgs:
        role = msg.get("role")
        content = msg.get("content", "")

        if role == "system":
            out.append({"role": "system", "content": content})
            continue

        # --- Assistant with content blocks (tool_use, text) ---
        if role == "assistant" and isinstance(content, list):
            text_parts = []
            tool_calls = []
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text":
                    text_parts.append(block["text"])
                elif block.get("type") == "tool_use":
                    tool_calls.append({
                        "id": block["id"],
                        "type": "function",
                        "function": {
                            "name": block["name"],
                            "arguments": json.dumps(block["input"]),
                        },
                    })

            m = {
                "role": "assistant",
                "content": "\n".join(text_parts) if text_parts else None,
            }
            if tool_calls:
                m["tool_calls"] = tool_calls
            out.append(m)
            continue

        # --- User with content blocks (tool_result, text, image, document) ---
        if role == "user" and isinstance(content, list):
            tool_results = []
            other_parts = []

            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_result":
                    tr_content = block.get("content", "")
                    if isinstance(tr_content, list):
                        # Same shape/reasoning as litellm.py's tool_result
                        # handling: OpenAI's "tool" role message doesn't
                        # support image content parts uniformly, so a
                        # placeholder satisfies the required
                        # tool_call_id/content pair and the actual image
                        # rides in `other_parts` as a following user-turn
                        # image.
                        tool_results.append({
                            "role": "tool",
                            "tool_call_id": block["tool_use_id"],
                            "content": "[image attached — see following message]",
                        })
                        for item in tr_content:
                            if item.get("type") == "image":
                                source = item.get("source", {})
                                media = source.get("media_type", "image/jpeg")
                                other_parts.append({
                                    "type": "image_url",
                                    "image_url": {"url": f"data:{media};base64,{source.get('data', '')}"},
                                })
                    else:
                        tool_results.append({
                            "role": "tool",
                            "tool_call_id": block["tool_use_id"],
                            "content": tr_content,
                        })
                elif block.get("type") == "text":
                    other_parts.append({"type": "text", "text": block["text"]})
                elif block.get("type") == "image":
                    source = block.get("source", {})
                    media = source.get("media_type", "image/jpeg")
                    other_parts.append({
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:{media};base64,{source.get('data', '')}",
                        },
                    })
                elif block.get("type") == "document":
                    other_parts.append({
                        "type": "text",
                        "text": "[PDF document attached]",
                    })

            out.extend(tool_results)

            if other_parts:
                if len(other_parts) == 1 and other_parts[0].get("type") == "text":
                    out.append({"role": "user", "content": other_parts[0]["text"]})
                else:
                    out.append({"role": "user", "content": other_parts})
            continue

        # --- Simple string content ---
        out.append({"role": role, "content": content})

    return out


# ---------------------------------------------------------------------------
# Responses API converters — reasoning OpenAI models (gpt-5.6-*) reject
# function tools on /v1/chat/completions while reasoning is active, same
# constraint documented in litellm.py. They go through client.responses.create
# instead, which supports reasoning + tools together. See registry.uses_responses_api.
# ---------------------------------------------------------------------------


def _msgs_to_responses(anthropic_msgs):
    """Anthropic-format message list -> Responses API input items.

    Same shape as litellm.py's converter of the same name — see that
    module's docstring for the per-block reasoning, except this path DOES
    replay thinking blocks: a reasoning item is emitted ahead of the
    function_call item(s) it preceded (see adapters/responses.py, which
    tags every thinking ContentBlock with the reasoning item's id +
    encrypted_content). This is what OpenAI's docs call manual conversation
    state management for reasoning models, and it's load-bearing under zero
    data retention — a ZDR org has `store` forced off server-side, so the
    only way the model keeps its chain-of-thought across a multi-tool-call
    turn is handing the encrypted blob back yourself.

    A thinking block with no id can't have come from this path (same idea
    as models/anthropic.py dropping unsigned thinking blocks) — drop it
    silently rather than send a malformed reasoning item.

    System messages go into `out` as role:"developer" items instead of a
    top-level `instructions` string — OpenAI's explicit prompt-caching docs
    are explicit that "top-level instructions cannot contain an explicit
    breakpoint," so a system_prompt_msg/memory_manifest_msg built with its
    own `cache_control` (see claude_loop_.py) would be untaggable if it
    stayed there. `cache_control`/`_ephemeral` are forwarded onto the
    output item unchanged (same convention as litellm.py's own converter)
    for _add_cache_breakpoints (below) to read — this function only
    reshapes the message, it doesn't decide what's breakpoint-worthy.
    """
    out = []
    for msg in anthropic_msgs:
        role = msg.get("role")
        content = msg.get("content", "")

        if role == "system":
            if isinstance(content, str) and content:
                item = {"role": "developer", "content": content}
                if msg.get("cache_control"):
                    item["cache_control"] = msg["cache_control"]
                out.append(item)
            continue

        if role == "assistant" and isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text" and block["text"]:
                    out.append({"role": "assistant", "content": block["text"]})
                elif block.get("type") == "tool_use":
                    out.append({
                        "type": "function_call",
                        "call_id": block["id"],
                        "name": block["name"],
                        "arguments": json.dumps(block["input"]),
                    })
                elif block.get("type") == "thinking" and block.get("id"):
                    item = {"type": "reasoning", "id": block["id"]}
                    item["summary"] = (
                        [{"type": "summary_text", "text": block["thinking"]}]
                        if block.get("thinking") else []
                    )
                    if block.get("signature"):
                        item["encrypted_content"] = block["signature"]
                    out.append(item)
            continue

        if role == "user" and isinstance(content, list):
            other_parts = []
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_result":
                    tr_content = block.get("content", "")
                    if isinstance(tr_content, list):
                        out.append({
                            "type": "function_call_output",
                            "call_id": block["tool_use_id"],
                            "output": "[image attached — see following message]",
                        })
                        for item in tr_content:
                            if item.get("type") == "image":
                                source = item.get("source", {})
                                media = source.get("media_type", "image/jpeg")
                                other_parts.append({
                                    "type": "input_image",
                                    "image_url": f"data:{media};base64,{source.get('data', '')}",
                                    "detail": "auto",
                                })
                    else:
                        out.append({
                            "type": "function_call_output",
                            "call_id": block["tool_use_id"],
                            "output": tr_content,
                        })
                elif block.get("type") == "text":
                    other_parts.append({"type": "input_text", "text": block["text"]})
                elif block.get("type") == "image":
                    source = block.get("source", {})
                    media = source.get("media_type", "image/jpeg")
                    other_parts.append({
                        "type": "input_image",
                        "image_url": f"data:{media};base64,{source.get('data', '')}",
                        "detail": "auto",
                    })
                elif block.get("type") == "document":
                    other_parts.append({"type": "input_text", "text": "[PDF document attached]"})

            if other_parts:
                out.append({"role": "user", "content": other_parts})
            continue

        # Simple string content — includes the ephemeral status tail
        # (claude_loop_.py's status_msgs), so _ephemeral must survive this
        # reshape for _add_cache_breakpoints' walk-back to see it.
        item = {"role": role, "content": content}
        if msg.get("_ephemeral"):
            item["_ephemeral"] = True
        out.append(item)

    return out


def _taggable_field(msg: dict) -> str | None:
    """Which field on `msg` can carry a content-block array (and therefore a
    prompt_cache_breakpoint): "content" for normal role-based messages,
    "output" for function_call_output (its type is `string |
    Array<ResponseInputText | ...>` per the Responses API — a plain string
    today, same as this app always sends, but block-capable). Plain
    function_call items only have "arguments" — a raw string field, not
    content-block-capable — so they return None and get skipped by the
    walk-back in _add_cache_breakpoints below.

    GPT-5.6+ explicit caching mechanism; item shapes match this app's own
    _msgs_to_responses output.
    """
    if msg.get("type") == "function_call_output":
        return "output"
    if "content" in msg:
        return "content"
    return None


def _tag_cache_breakpoint(msg: dict) -> dict:
    """Return a copy of `msg` with its taggable field (see _taggable_field)
    converted to block shape and the last block tagged
    prompt_cache_breakpoint — never mutates the caller's dict (the caller
    reuses these dicts across loop iterations, so an in-place tag would
    leave stale breakpoints on old messages once new ones get appended
    after it)."""
    field = _taggable_field(msg)
    if field is None:
        return msg

    tagged = dict(msg)
    value = tagged.get(field)
    if isinstance(value, str):
        tagged[field] = [
            {
                "type": "input_text",
                "text": value,
                "prompt_cache_breakpoint": {"mode": "explicit"},
            }
        ]
    elif isinstance(value, list) and value:
        new_value = list(value)
        last_block = dict(new_value[-1]) if isinstance(new_value[-1], dict) else {}
        last_block["prompt_cache_breakpoint"] = {"mode": "explicit"}
        new_value[-1] = last_block
        tagged[field] = new_value
    return tagged


def _add_cache_breakpoints(input_list: list) -> list:
    """GPT-5.6+ explicit prompt caching: tag every message the CALLER
    explicitly marked with a truthy "cache_control" key (mirrors the
    Anthropic apps' own per-block cache_control convention exactly — see
    models/anthropic.py in this repo), plus one rolling breakpoint on the
    last STABLE taggable item in the transcript (mirrors the bp_idx
    walk-back there too).

    Explicit, caller-set tags rather than a positional "last
    system/developer message" scan — claude_loop_.py builds at least two
    independently-changing role:"developer"/"system" blocks (the core
    system_prompt_msg and the memory_manifest_msg, which changes turn to
    turn while the system prompt doesn't), and a position scan can't tell
    them apart. claude_loop_.py decides what's breakpoint-worthy (by
    setting cache_control on system_prompt_msg/memory_manifest_msg); this
    function just applies it. It replaced an earlier
    positional-scan version shared one breakpoint between the system
    prompt and the memory manifest, so every memory edit busted the cache
    for both).

    A message flagged "_ephemeral" (content that changes on every call —
    e.g. claude_loop_.py's status_msgs tail) is skipped by the
    rolling-breakpoint walk-back: tagging one would make the cached prefix
    un-reusable on the very next call and discard the whole transcript. A
    bare function_call item (arguments-only, not content-block-capable —
    see _taggable_field) can legitimately be the literal last item when a
    turn ends mid-tool-call; the walk-back skips those too.

    "cache_control"/"_ephemeral" are internal markers only — stripped from
    every message before it reaches the API, tagged or not, since they're
    not fields the Responses API recognizes.

    Every model that reaches this function via _responses_call already
    supports prompt_cache_breakpoint (see registry.uses_responses_api —
    only the gpt-5.6-*/gpt-6-astra family has responses_api: True), so
    there's no per-call model check needed here.
    """
    if not input_list:
        return input_list

    explicit_idxs = {i for i, msg in enumerate(input_list) if msg.get("cache_control")}

    last_taggable_idx = None
    for i in range(len(input_list) - 1, -1, -1):
        if input_list[i].get("_ephemeral"):
            continue
        if _taggable_field(input_list[i]) is not None:
            last_taggable_idx = i
            break

    tagged_list = []
    for i, msg in enumerate(input_list):
        if i in explicit_idxs or i == last_taggable_idx:
            msg = _tag_cache_breakpoint(msg)
        else:
            msg = dict(msg)
        msg.pop("cache_control", None)
        msg.pop("_ephemeral", None)
        tagged_list.append(msg)
    return tagged_list


async def _responses_call(
    client,
    model,
    input,
    encoded_image,
    tools,
    stream,
    max_tokens,
    pdf,
    retries,
    base_sleep,
):
    """Reasoning-model call path via client.responses.create (see registry.uses_responses_api)."""
    # Explicit prompt caching only applies to the main conversational path
    # (not the one-shot pdf/encoded_image/bare-string calls below, which are
    # single small requests with nothing worth caching) — see
    # _add_cache_breakpoints' own docstring for why every model reaching
    # this function is gpt-5.6+ eligible with no further check needed.
    use_cache_tagging = False

    if pdf:
        resp_input = f"[PDF document attached]\n\n{input}"
    elif encoded_image:
        content = []
        imgs = [encoded_image] if isinstance(encoded_image, str) else encoded_image
        for img in imgs:
            content.append({
                "type": "input_image",
                "image_url": f"data:image/jpeg;base64,{img}",
                "detail": "auto",
            })
        content.append({"type": "input_text", "text": input})
        resp_input = [{"role": "user", "content": content}]
    elif isinstance(input, str):
        resp_input = input
    else:
        resp_input = _add_cache_breakpoints(_msgs_to_responses(input))
        use_cache_tagging = True

    api_params = {
        "model": model,
        "input": resp_input,
        "max_output_tokens": max_tokens,
        "stream": stream,
        "reasoning": {"effort": "high", "summary": "auto"},
        # Needed to get the reasoning item's encrypted_content back at all —
        # without it, _msgs_to_responses has only the item id to replay,
        # which OpenAI can only resolve server-side when store isn't forced
        # off (not guaranteed under this account's ZDR posture).
        "include": ["reasoning.encrypted_content"],
    }
    if use_cache_tagging:
        api_params["prompt_cache_options"] = {"mode": "explicit"}

    if tools:
        responses_tools = tools_to_responses(tools)
        if responses_tools:
            api_params["tools"] = responses_tools
            api_params["tool_choice"] = "auto"

    for attempt in range(retries):
        try:
            response = await client.responses.create(**api_params)
            if stream:
                return await prime_stream(responses.stream_response(response), first_event_timeout(FIRST_EVENT_TIMEOUT))
            else:
                return responses.response(response)

        except Exception as e:
            import traceback
            print(f"\n[openai responses_call]: {e}", flush=True)
            traceback.print_exc()
            if attempt < retries - 1:
                sleep_time = base_sleep * (2 ** attempt)
                print(
                    f"\n[openai responses_call]: Retrying in {sleep_time}s (attempt {attempt + 1}/{retries})..."
                )
                await asyncio.sleep(sleep_time)
            else:
                print(f"\n[openai responses_call]: Failed after {retries} attempts")

    return None


# ---------------------------------------------------------------------------
# Main model call — same signature as models/anthropic.py
# ---------------------------------------------------------------------------

async def oai_model_call(
    input: Union[List[Dict[str, Any]], str],
    model=DEFAULT_MODEL,
    encoded_image: Optional[Union[str, List[str]]] = None,
    tools: Optional[List[Dict[str, Any]]] = None,
    stream: bool = False,
    thinking=False,
    max_tokens: int | None = None,
    client_timeout: int = 480,
    pdf: str = None,
    retries: int = 3,
):
    client = AsyncOpenAI(
        base_url=os.getenv("OPENAI_BASE_URL") or None,
        api_key=os.getenv("OPENAI_API_KEY", ""),
        timeout=client_timeout,
    )
    base_sleep = 2

    alias = model
    # Registry-derived output ceiling when the caller didn't pass one
    # explicitly — was a flat 120000 regardless of alias. 120000 stays only
    # as the fallback for an unregistered alias.
    max_tokens = max_tokens or max_output_for(alias) or 120000
    model = resolve(alias, "openai")

    if uses_responses_api(alias):
        return await _responses_call(
            client=client,
            model=model,
            input=input,
            encoded_image=encoded_image,
            tools=tools,
            stream=stream,
            max_tokens=max_tokens,
            pdf=pdf,
            retries=retries,
            base_sleep=base_sleep,
        )

    # ---- Build messages ----
    messages = []

    if pdf:
        # No native PDF block on chat.completions — pass as note.
        messages = [{"role": "user", "content": f"[PDF document attached]\n\n{input}"}]
    elif encoded_image:
        content = []
        imgs = [encoded_image] if isinstance(encoded_image, str) else encoded_image
        for img in imgs:
            content.append({
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{img}"},
            })
        content.append({"type": "text", "text": input})
        messages = [{"role": "user", "content": content}]
    elif isinstance(input, str):
        messages = [{"role": "user", "content": input}]
    else:
        messages = _msgs_to_openai(input)
        # strip the helper flag — the OpenAI client rejects unknown message keys
        for m in messages:
            m.pop("_ephemeral", None)

    # ---- API parameters ----
    api_params = {
        "model": model,
        "max_tokens": max_tokens,
        "stream": stream,
        "messages": messages,
    }

    if stream:
        api_params["stream_options"] = {"include_usage": True}

    if thinking and supports_thinking(alias):
        api_params["reasoning_effort"] = "high"

    if tools:
        openai_tools = tools_to_chat(tools)
        if openai_tools:
            api_params["tools"] = openai_tools
            api_params["tool_choice"] = "auto"

    # ---- Call with retries ----
    for attempt in range(retries):
        try:
            response = await client.chat.completions.create(**api_params)
            if stream:
                return await prime_stream(chat.stream_response(response), first_event_timeout(FIRST_EVENT_TIMEOUT))
            else:
                return chat.response(response)

        except Exception as e:
            import traceback
            print(f"\n[openai model_call]: {e}", flush=True)
            traceback.print_exc()
            if attempt < retries - 1:
                sleep_time = base_sleep * (2 ** attempt)
                print(
                    f"\n[openai model_call]: Retrying in {sleep_time}s (attempt {attempt + 1}/{retries})..."
                )
                await asyncio.sleep(sleep_time)
            else:
                print(f"\n[openai model_call]: Failed after {retries} attempts")

    return None
