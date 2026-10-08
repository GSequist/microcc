"""LiteLLM model caller.

OpenAI-compatible endpoint (LiteLLM-style) wrapping Bedrock Claude.
Translates between Anthropic message format (used by claude_loop_)
and OpenAI chat completion format (used by the proxy).

Env vars:
    LITELLM_BASE_URL: Proxy base URL
    LITELLM_API_KEY:  API key for Authorization: Bearer header
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
    wants_summarized_display,
    uses_responses_api,
    anthropic_beta_headers,
    max_output_for,
)

load_dotenv(os.path.expanduser("~/.micro-cc/.env"))
load_dotenv()

# Seconds to wait for a streaming call's first event before treating a
# connected-but-silent proxy as a failed attempt — see priming.prime_stream.
FIRST_EVENT_TIMEOUT = 300
# 60s was too tight for proxies: a slow first event is retried by re-sending the
# whole prompt. Override for all providers with MICROCC_FIRST_EVENT_TIMEOUT.


# ---------------------------------------------------------------------------
# Format converters
# ---------------------------------------------------------------------------


def _msgs_to_openai(anthropic_msgs):
    """Convert Anthropic-format message list to OpenAI chat format.

    Handles: system, user (string/multimodal/tool_result),
    assistant (string/tool_use+thinking blocks).
    Images converted to OpenAI image_url format.
    """
    out = []
    for msg in anthropic_msgs:
        role = msg.get("role")
        content = msg.get("content", "")

        # System messages pass through. If the caller tagged a cache_control
        # breakpoint, emit content as a single text block carrying it — LiteLLM
        # forwards cache_control on content blocks to Bedrock/Anthropic.
        if role == "system":
            if msg.get("cache_control") and isinstance(content, str):
                out.append({
                    "role": "system",
                    "content": [{
                        "type": "text",
                        "text": content,
                        "cache_control": msg["cache_control"],
                    }],
                })
            else:
                out.append({"role": "system", "content": content})
            continue

        # --- Assistant with content blocks (tool_use, text, thinking) ---
        if role == "assistant" and isinstance(content, list):
            text_parts = []
            tool_calls = []
            thinking_blocks = []
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
                elif block.get("type") == "thinking":
                    # LiteLLM requires thinking_blocks resent on assistant messages
                    thinking_blocks.append({
                        "type": "thinking",
                        "thinking": block.get("thinking", ""),
                        "signature": block.get("signature", ""),
                    })

            m = {
                "role": "assistant",
                "content": "\n".join(text_parts) if text_parts else None,
            }
            if tool_calls:
                m["tool_calls"] = tool_calls
            if thinking_blocks:
                m["thinking_blocks"] = thinking_blocks
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
                        # Only shape so far: read_'s image result (see
                        # file_tools_.py) wrapped by claude_loop_.py into a
                        # real tool_result image block. OpenAI's "tool" role
                        # message doesn't support image content parts
                        # uniformly across providers — placeholder text
                        # satisfies the required tool_call_id/content pair,
                        # and the actual image rides in `other_parts` below
                        # (same translation path as a direct user-content
                        # image), landing as a normal user-turn image right
                        # after this tool result, so the model still sees it.
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
                    # Document blocks from tool results (vectorstore citations etc.)
                    # No OpenAI equivalent — convert to text placeholder
                    other_parts.append({
                        "type": "text",
                        "text": "[PDF document attached]",
                    })

            # Tool results become separate messages
            out.extend(tool_results)

            # Remaining content becomes a user message
            if other_parts:
                if len(other_parts) == 1 and other_parts[0].get("type") == "text":
                    out.append({"role": "user", "content": other_parts[0]["text"]})
                else:
                    out.append({"role": "user", "content": other_parts})
            continue

        # --- Simple string content ---
        m = {"role": role, "content": content}
        if msg.get("_ephemeral"):
            # carry the flag through so the breakpoint logic can skip it
            # (stripped before the API call); see l_model_call.
            m["_ephemeral"] = True
        out.append(m)

    return out


# ---------------------------------------------------------------------------
# Responses API converters — gpt-5.6-* reject function tools on
# /v1/chat/completions while reasoning is active ("Function tools with
# reasoning_effort are not supported ... use /v1/responses"). These models
# route through client.responses.create instead, which supports reasoning +
# tools together. See registry.uses_responses_api.
# ---------------------------------------------------------------------------


def _msgs_to_responses(anthropic_msgs):
    """Anthropic-format message list -> (instructions, Responses API input items).

    System message becomes the top-level `instructions` string. Assistant
    tool_use/text blocks and user tool_result/text/image blocks each become
    their own input item (function_call, function_call_output, message).
    Thinking blocks are dropped — verified the proxy re-derives its own
    reasoning each turn from the transcript; no signature/id replay needed
    for multi-turn tool calling to work.
    """
    instructions_parts = []
    out = []
    for msg in anthropic_msgs:
        role = msg.get("role")
        content = msg.get("content", "")

        if role == "system":
            # Callers may send more than one system message now (e.g.
            # claude_loop_'s system_prompt_msg + memory_manifest_msg, each
            # its own cache_control-bearing block for the Anthropic-native
            # path) — the Responses API has no such multi-block system
            # concept, so join them into the one `instructions` string in
            # message order instead of last-write-wins dropping everything
            # but the last one.
            if isinstance(content, str) and content:
                instructions_parts.append(content)
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
            continue

        if role == "user" and isinstance(content, list):
            other_parts = []
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_result":
                    tr_content = block.get("content", "")
                    if isinstance(tr_content, list):
                        # Same shape/reasoning as _msgs_to_openai's tool_result
                        # handling above — function_call_output.output is a
                        # plain string, so an image result gets a placeholder
                        # here and the real image rides in other_parts as a
                        # following input_image, same as a direct user image.
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

        # --- Simple string content (user/assistant plain turns) ---
        out.append({"role": role, "content": content})

    return ("\n\n".join(instructions_parts) if instructions_parts else None), out


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
    """gpt-5.6-* call path via client.responses.create (see uses_responses_api)."""
    instructions = None

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
        instructions, resp_input = _msgs_to_responses(input)

    api_params = {
        "model": model,
        "input": resp_input,
        "max_output_tokens": max_tokens,
        "stream": stream,
        "reasoning": {"effort": "high", "summary": "auto"},
    }
    if instructions:
        api_params["instructions"] = instructions

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
            print(f"\n[litellm responses_call]: {e}", flush=True)
            traceback.print_exc()
            if attempt < retries - 1:
                sleep_time = base_sleep * (2 ** attempt)
                print(
                    f"\n[litellm responses_call]: Retrying in {sleep_time}s (attempt {attempt + 1}/{retries})..."
                )
                await asyncio.sleep(sleep_time)
            else:
                print(f"\n[litellm responses_call]: Failed after {retries} attempts")

    return None


# ---------------------------------------------------------------------------
# Main model call — same signature as models/anthropic.py
# ---------------------------------------------------------------------------

async def l_model_call(
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
        base_url=os.getenv(
            "LITELLM_BASE_URL", ""
        ),
        api_key=os.getenv("LITELLM_API_KEY", ""),
        timeout=client_timeout,
    )
    base_sleep = 2

    alias = model
    # Registry-derived output ceiling when the caller didn't pass one
    # explicitly — was a flat 120000 regardless of alias, which over-asks
    # an older 64k-max_output alias on its bedrock route through this same
    # proxy (the Haiku trap). 120000 stays only as the fallback for an
    # unregistered alias.
    max_tokens = max_tokens or max_output_for(alias) or 120000
    model = resolve(alias, "litellm")

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
        # Proxy doesn't support native PDF blocks; pass as note
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
        # Conversation history — convert from Anthropic format
        # System msgs pass through as role: "system" inline in the array
        messages = _msgs_to_openai(input)

        # rolling cache breakpoint: tag the last message so repeat loops read
        # the prior transcript from cache. Walk back past ephemeral trailing
        # messages (the todos reminder) — its content changes every call, so
        # caching it would discard the transcript prefix next turn. Also walk
        # back past "tool" role — OpenAI tool messages require string content,
        # so they can't carry cache_control; landing on one used to just skip
        # tagging entirely for that call instead of continuing the walk, which
        # meant the breakpoint stopped advancing on almost every iteration of
        # a tool-heavy turn (the message right before the ephemeral tail is a
        # tool result on nearly every loop iteration). Keep walking until an
        # eligible anchor (a real assistant/user message) is found.
        bp_idx = len(messages) - 1
        while bp_idx > 0 and (
            messages[bp_idx].get("_ephemeral") or messages[bp_idx].get("role") == "tool"
        ):
            bp_idx -= 1
        if (
            messages
            and not messages[bp_idx].get("_ephemeral")
            and messages[bp_idx].get("role") != "tool"
        ):
            last = dict(messages[bp_idx])
            lc = last.get("content")
            if isinstance(lc, str) and lc:
                last["content"] = [
                    {
                        "type": "text",
                        "text": lc,
                        "cache_control": {"type": "ephemeral"},
                    }
                ]
                messages[bp_idx] = last
            elif isinstance(lc, list) and lc and isinstance(lc[-1], dict):
                new_lc = list(lc)
                new_lc[-1] = {**lc[-1], "cache_control": {"type": "ephemeral"}}
                last["content"] = new_lc
                messages[bp_idx] = last

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
        api_params["extra_body"] = {
            "thinking": {"type": "adaptive"},
            "output_config": {"effort": "high"},
        }
        if wants_summarized_display(alias):
            api_params["extra_body"]["thinking"]["display"] = "summarized"

    # 1M-context beta on Bedrock is body-field-gated, not header-gated like
    # direct Anthropic — Bedrock's Anthropic-compatible surface takes
    # "anthropic_beta" as a JSON field, same names/semantics as the direct
    # API's anthropic-beta header (see registry.anthropic_beta_headers). Not
    # independently verified against a live Bedrock response the way
    # anthropic.py's header path is — cheap to include, and matches this
    # same extra_body pass-through the thinking/output_config fields above
    # already rely on to reach Bedrock through the proxy.
    betas = anthropic_beta_headers(alias, server_fallback=False)
    if betas:
        api_params.setdefault("extra_body", {})["anthropic_beta"] = betas

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
                return await prime_stream(
                    chat.stream_response(response, reasoning="litellm"), first_event_timeout(FIRST_EVENT_TIMEOUT)
                )
            else:
                return chat.response(response, reasoning="litellm")

        except Exception as e:
            import traceback
            print(f"\n[litellm model_call]: {e}", flush=True)
            traceback.print_exc()
            if attempt < retries - 1:
                sleep_time = base_sleep * (2 ** attempt)
                print(
                    f"\n[litellm model_call]: Retrying in {sleep_time}s (attempt {attempt + 1}/{retries})..."
                )
                await asyncio.sleep(sleep_time)
            else:
                print(f"\n[litellm model_call]: Failed after {retries} attempts")

    return None
