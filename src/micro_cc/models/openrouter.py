"""OpenRouter requests via its OpenAI-compatible endpoint.

No fixed alias table (models/registry.py) — the model is whatever
"author/slug" the user pasted at /login. resolve() still runs but just
passes it through unchanged. Reasoning uses OpenRouter's unified `reasoning`
param instead of a provider-specific toggle.

Env vars:
    OPENROUTER_API_KEY:  API key (openrouter.ai/keys)
    OPENROUTER_BASE_URL: default https://openrouter.ai/api/v1
"""

from typing import List, Dict, Any, Optional, Union
from openai import AsyncOpenAI
from dotenv import load_dotenv
import asyncio
import json
import os

from micro_cc.models.adapters import chat
from micro_cc.models.priming import prime_stream, first_event_timeout
from micro_cc.models.schema import tools_to_chat
from micro_cc.models.registry import DEFAULT_MODEL, resolve, supports_thinking

load_dotenv(os.path.expanduser("~/.micro-cc/.env"))
load_dotenv()

FIRST_EVENT_TIMEOUT = 180
# 60s was too tight for proxies: a slow first event is retried by re-sending the
# whole prompt. Override for all providers with MICROCC_FIRST_EVENT_TIMEOUT.


# ---------------------------------------------------------------------------
# Converters — Anthropic format -> OpenAI chat format
# ---------------------------------------------------------------------------


def _msgs_to_openai(anthropic_msgs):
    """Anthropic-format message list -> OpenAI chat format.

    Thinking blocks round-trip as OpenRouter's own `reasoning` field on the
    assistant message — the same field its responses surface the text on
    (see adapters/chat.py's "openrouter" branch), so this is symmetric with
    what OpenRouter itself sends back, not a guess at wire shape. Unlike
    Anthropic's thinking blocks there's no signature to validate: OpenRouter
    doesn't reject an unsigned/plain-text reasoning replay, so this is a
    best-effort resend (some backend models use it, others ignore it) rather
    than something load-bearing for a 400 the way Anthropic's is.
    """
    out = []
    for msg in anthropic_msgs:
        role = msg.get("role")
        content = msg.get("content", "")

        if role == "system":
            out.append({"role": "system", "content": content})
            continue

        if role == "assistant" and isinstance(content, list):
            text_parts = []
            tool_calls = []
            reasoning_text = ""
            for block in content:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "text":
                    text_parts.append(block["text"])
                elif btype == "tool_use":
                    tool_calls.append({
                        "id": block["id"],
                        "type": "function",
                        "function": {
                            "name": block["name"],
                            "arguments": json.dumps(block["input"]),
                        },
                    })
                elif btype == "thinking":
                    reasoning_text = block.get("thinking", "")

            m = {
                "role": "assistant",
                "content": "\n".join(text_parts) if text_parts else None,
            }
            if tool_calls:
                m["tool_calls"] = tool_calls
            if reasoning_text:
                m["reasoning"] = reasoning_text
            out.append(m)
            continue

        if role == "user" and isinstance(content, list):
            tool_results = []
            other_parts = []

            for block in content:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "tool_result":
                    tr_content = block.get("content", "")
                    if isinstance(tr_content, list):
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
                elif btype == "text":
                    other_parts.append({"type": "text", "text": block["text"]})
                elif btype == "image":
                    source = block.get("source", {})
                    media = source.get("media_type", "image/jpeg")
                    other_parts.append({
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:{media};base64,{source.get('data', '')}",
                        },
                    })
                elif btype == "document":
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

        out.append({"role": role, "content": content})

    return out


# ---------------------------------------------------------------------------
# Main entry point — same signature pattern as models/ollama.py
# ---------------------------------------------------------------------------


async def or_model_call(
    input: Union[List[Dict[str, Any]], str],
    model=DEFAULT_MODEL,
    encoded_image: Optional[Union[str, List[str]]] = None,
    tools: Optional[List[Dict[str, Any]]] = None,
    stream: bool = False,
    thinking: bool = False,
    max_tokens: int = 120000,
    client_timeout: int = 480,
    pdf: str = None,
    retries: int = 3,
):
    base_url = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
    client = AsyncOpenAI(
        base_url=base_url,
        api_key=os.getenv("OPENROUTER_API_KEY", ""),
        timeout=client_timeout,
    )
    base_sleep = 2

    alias = model
    model = resolve(alias, "openrouter")

    # ---- Build messages ----
    if pdf:
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

    # ---- API parameters ----
    api_params = {
        "model": model,
        "max_tokens": max_tokens,
        "stream": stream,
        "messages": messages,
    }

    if stream:
        api_params["stream_options"] = {"include_usage": True}

    # Non-standard params (vLLM/HF-ecosystem, not part of OpenAI's own API
    # surface) must ride in extra_body — the openai SDK's typed
    # chat.completions.create() raises a TypeError on an unknown top-level
    # kwarg before a request is even sent, it doesn't pass it through.
    extra_body = {}

    if thinking and supports_thinking(alias):
        # OpenRouter's unified reasoning param — a no-op on unsupported models.
        extra_body["reasoning"] = {"effort": "high"}

    if extra_body:
        api_params["extra_body"] = extra_body

    if tools:
        openrouter_tools = tools_to_chat(tools)
        if openrouter_tools:
            api_params["tools"] = openrouter_tools
            api_params["tool_choice"] = "auto"

    # ---- Call with retries ----
    for attempt in range(retries):
        try:
            response = await client.chat.completions.create(**api_params)
            if stream:
                return await prime_stream(
                    chat.stream_response(response, reasoning="openrouter"), first_event_timeout(FIRST_EVENT_TIMEOUT)
                )
            else:
                return chat.response(response, reasoning="openrouter")

        except Exception as e:
            import traceback
            print(f"\n[openrouter model_call]: {e}", flush=True)
            traceback.print_exc()
            if attempt < retries - 1:
                sleep_time = base_sleep * (2 ** attempt)
                print(
                    f"\n[openrouter model_call]: Retrying in {sleep_time}s (attempt {attempt + 1}/{retries})..."
                )
                await asyncio.sleep(sleep_time)
            else:
                print(f"\n[openrouter model_call]: Failed after {retries} attempts")

    return None
