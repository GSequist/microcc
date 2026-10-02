"""Ollama requests via its OpenAI-compatible endpoint.

Request conversion and Ollama's think/num_ctx options live here. Shared chat
output decoding lives in adapters/chat.py with Ollama reasoning extraction.

Env vars:
    OLLAMA_BASE_URL   default http://localhost:11434/v1
    OLLAMA_MODEL      user picks any tag pulled via `ollama pull <tag>`
    OLLAMA_NUM_CTX    default 32768 — fits Qwen3-14B KV cache in ~5GB
    OLLAMA_MAX_OUTPUT default 4096  — generation budget per response
    OLLAMA_FIRST_EVENT_TIMEOUT default 180 — local prefill on modest hardware
                      can legitimately take minutes before the first token;
                      see priming.prime_stream
"""

from typing import List, Dict, Any, Optional, Union
from openai import AsyncOpenAI
from dotenv import load_dotenv
import asyncio
import json
import os

from micro_cc.models.adapters import chat
from micro_cc.models.priming import prime_stream
from micro_cc.models.schema import tools_to_chat

load_dotenv(os.path.expanduser("~/.micro-cc/.env"))
load_dotenv()


# ---------------------------------------------------------------------------
# Converters — Anthropic format → OpenAI chat format
# ---------------------------------------------------------------------------


def _msgs_to_openai(anthropic_msgs):
    """Convert Anthropic-format message list to OpenAI chat format.

    Handles: system, user (string/multimodal/tool_result),
    assistant (string/tool_use blocks). Thinking blocks are dropped —
    local models can't replay signed thinking, and Ollama's OpenAI-compat
    endpoint doesn't accept them on the resend path.
    """
    out = []
    for msg in anthropic_msgs:
        role = msg.get("role")
        content = msg.get("content", "")

        if role == "system":
            out.append({"role": "system", "content": content})
            continue

        # --- Assistant with content blocks ---
        if role == "assistant" and isinstance(content, list):
            text_parts = []
            tool_calls = []
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
                # thinking blocks: intentionally skipped for Ollama

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
                btype = block.get("type")
                if btype == "tool_result":
                    tr_content = block.get("content", "")
                    if isinstance(tr_content, list):
                        # read_'s image result (see file_tools_.py) wrapped
                        # into a real tool_result image block — Ollama's
                        # "tool" role message doesn't take image content
                        # parts, so a placeholder satisfies content here and
                        # the real image rides in other_parts below, landing
                        # as a normal user-turn image right after.
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

        # --- Simple string content ---
        out.append({"role": role, "content": content})

    return out


# ---------------------------------------------------------------------------
# Main entry point — same signature pattern as models/anthropic.py
# ---------------------------------------------------------------------------

async def o_model_call(
    input: Union[List[Dict[str, Any]], str],
    model: str = None,
    encoded_image: Optional[Union[str, List[str]]] = None,
    tools: Optional[List[Dict[str, Any]]] = None,
    stream: bool = False,
    thinking: bool = False,
    max_tokens: int = None,
    client_timeout: int = 480,
    pdf: str = None,
    retries: int = 3,
):
    base_url = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434/v1")
    model = model or os.getenv("OLLAMA_MODEL")
    num_ctx = int(os.getenv("OLLAMA_NUM_CTX", "32768"))
    if max_tokens is None:
        max_tokens = int(os.getenv("OLLAMA_MAX_OUTPUT", "4096"))
    first_event_timeout = float(os.getenv("OLLAMA_FIRST_EVENT_TIMEOUT", "180"))

    client = AsyncOpenAI(
        base_url=base_url,
        api_key="ollama",  # dummy — Ollama ignores the key
        timeout=client_timeout,
    )
    base_sleep = 2

    # ---- Build messages ----
    messages = []
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
    # Ollama-specific: num_ctx in extra_body.options, think toggle at root
    extra_body = {"options": {"num_ctx": num_ctx}}
    if thinking:
        extra_body["think"] = True

    api_params = {
        "model": model,
        "max_tokens": max_tokens,
        "stream": stream,
        "messages": messages,
        "extra_body": extra_body,
    }

    if stream:
        api_params["stream_options"] = {"include_usage": True}

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
                    chat.stream_response(response, reasoning="ollama"), first_event_timeout
                )
            else:
                return chat.response(response, reasoning="ollama")

        except Exception as e:
            import traceback
            print(f"\n[ollama model_call]: {e}", flush=True)
            traceback.print_exc()
            if attempt < retries - 1:
                sleep_time = base_sleep * (2 ** attempt)
                print(
                    f"\n[ollama model_call]: Retrying in {sleep_time}s (attempt {attempt + 1}/{retries})..."
                )
                await asyncio.sleep(sleep_time)
            else:
                print(f"\n[ollama model_call]: Failed after {retries} attempts")

    return None
