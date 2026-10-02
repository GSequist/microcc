"""Foundry model caller.

Same wire format as models/anthropic.py (AsyncAnthropic SDK,
client.beta.messages.create) — Foundry just fronts a different base_url
with its own api_key, not a different message/tool/thinking shape.

Env vars:
    AZURE_FOUNDRY_URL_CLAUDE: Foundry Claude endpoint base URL
    AZURE_FOUNDRY_API_KEY:    API key for the Foundry endpoint
"""

from typing import List, Dict, Any, Optional, Union
from anthropic import AsyncAnthropic
from dotenv import load_dotenv
import asyncio
import os

from micro_cc.models.adapters.anthropic import response as wrap_response, stream_response
from micro_cc.models.priming import prime_stream, first_event_timeout
from micro_cc.models.registry import DEFAULT_MODEL, resolve, supports_thinking, wants_summarized_display, max_output_for

load_dotenv(os.path.expanduser("~/.micro-cc/.env"))
load_dotenv()

# Seconds to wait for a streaming call's first event before treating a
# connected-but-silent endpoint as a failed attempt — see priming.prime_stream.
FIRST_EVENT_TIMEOUT = 120
# 60s was too tight for proxies: a slow first event is retried by re-sending the
# whole prompt. Override for all providers with MICROCC_FIRST_EVENT_TIMEOUT.


async def f_model_call(
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
    client = AsyncAnthropic(
        base_url=os.getenv("FOUNDRY_BASE_URL", ""),
        api_key=os.getenv("FOUNDRY_API_KEY", ""),
        timeout=client_timeout,
    )
    base_sleep = 2

    alias = model
    # Registry-derived output ceiling when the caller didn't pass one
    # explicitly — see the matching comment in models/anthropic.py. 60000
    # stays only as the fallback for an unregistered alias.
    max_tokens = max_tokens or max_output_for(alias) or 60000
    model = resolve(alias, "foundry")

    system_prompts = []
    messages = []
    if pdf:
        messages = [
            {
                "role": "user",
                "content": [
                    {
                        "type": "document",
                        "source": {
                            "type": "base64",
                            "media_type": "application/pdf",
                            "data": pdf,
                        },
                    },
                    {
                        "type": "text",
                        "text": input,
                    },
                ],
            }
        ]
    elif encoded_image:
        content = []
        if isinstance(encoded_image, str):
            content.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/jpeg",
                        "data": encoded_image,
                    },
                }
            )
        else:
            for img in encoded_image:
                content.append(
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/jpeg",
                            "data": img,
                        },
                    }
                )

        content.append({"type": "text", "text": input})
        messages = [{"role": "user", "content": content}]
    elif isinstance(input, str):
        messages = [{"role": "user", "content": input}]
    else:
        # ephemeral_flags[i] mirrors messages[i]: True for transcript messages
        # whose content changes every call (e.g. the todos reminder). They must
        # not carry the rolling cache breakpoint. The '_ephemeral' key never
        # reaches the API — we only copy role+content into messages.
        ephemeral_flags = []
        for msg in input:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            if role == "system":
                # keep system messages as ordered content blocks (do NOT
                # collapse to one joined string) so a cache_control breakpoint
                # set by the caller survives. The prefix up to the block
                # carrying cache_control (tools + system) gets cached.
                if isinstance(content, str) and content:
                    block = {"type": "text", "text": content}
                    if msg.get("cache_control"):
                        block["cache_control"] = msg["cache_control"]
                    system_prompts.append(block)
                continue
            if isinstance(content, list):
                # Same shape as models/anthropic.py: drop a thinking block
                # with no signature (never came from Anthropic — see that
                # file's comment), and strip claude_loop_'s extra "id" key
                # (added for OpenAI Responses reasoning replay) from the
                # rest — Foundry's BetaThinkingBlockParam only accepts
                # type/thinking/signature; a modified block 400s the request.
                content = [
                    ({"type": "thinking", "thinking": b.get("thinking", ""), "signature": b["signature"]}
                     if isinstance(b, dict) and b.get("type") == "thinking" else b)
                    for b in content
                    if not (isinstance(b, dict) and b.get("type") == "thinking" and not b.get("signature"))
                ]
            messages.append({"role": role, "content": content})
            ephemeral_flags.append(bool(msg.get("_ephemeral")))

        # rolling cache breakpoint on the transcript: tag the last STABLE message
        # so loops 2..N read prior turns from cache. Walk back past ephemeral
        # trailing messages (e.g. the todos reminder) — caching one would make
        # the cached prefix un-reusable next turn and discard the transcript.
        # Copy instead of mutating — the caller reuses these dicts across loops,
        # and an in-place tag would leave stale breakpoints (>4 -> 400).
        bp_idx = len(messages) - 1
        while bp_idx > 0 and ephemeral_flags[bp_idx]:
            bp_idx -= 1
        if messages and not ephemeral_flags[bp_idx]:
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

    api_parameters = {"model": model}

    if system_prompts:
        api_parameters["system"] = system_prompts

    if tools:
        api_parameters["tools"] = tools
        api_parameters["tool_choice"] = {
            "type": "auto",
            "disable_parallel_tool_use": False,
        }

    api_parameters["max_tokens"] = max_tokens

    if thinking and supports_thinking(alias):
        api_parameters["thinking"] = {"type": "adaptive"}
        if wants_summarized_display(alias):
            api_parameters["thinking"]["display"] = "summarized"
        api_parameters["output_config"] = {"effort": "high"}

    api_parameters["messages"] = messages
    api_parameters["stream"] = stream

    for attempt in range(retries):
        try:
            response = await client.beta.messages.create(**api_parameters)
            if stream:
                return await prime_stream(stream_response(response), first_event_timeout(FIRST_EVENT_TIMEOUT))
            return wrap_response(response)

        except Exception as e:
            print(f"\n[foundry model_call]: {e}", flush=True)
            if attempt < retries - 1:
                sleep_time = base_sleep * (2**attempt)
                print(
                    f"\n[foundry model_call]: Retrying in {sleep_time}s (attempt {attempt + 1}/{retries})..."
                )
                await asyncio.sleep(sleep_time)
            else:
                print(f"\n[foundry model_call]: Failed after {retries} attempts")

    return None
