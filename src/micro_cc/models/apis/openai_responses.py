"""OpenAI Responses wire: OpenAI direct, and GPT models behind LiteLLM.

Reasoning models reject function tools on /v1/chat/completions while reasoning is active,
so they go through client.responses.create. Two compat flags split the providers:
  replay_reasoning  resend reasoning items (id + encrypted_content) so chain-of-thought
                    survives multi-tool turns under zero data retention (store forced off)
  explicit_cache    system prompts as developer items with prompt_cache_breakpoint tags;
                    without it they join into the top-level `instructions` string
"""

import json

from openai import AsyncOpenAI

from micro_cc.models.adapters import responses
from micro_cc.models.catalog import PROVIDERS, compat, max_output_for, resolve_effort
from micro_cc.models.client import cached_client
from micro_cc.models.providers import credentials
from micro_cc.models.retry import first_event_timeout, retry_call
from micro_cc.models.schema import tools_to_responses

FALLBACK_MAX_TOKENS = 120000


def _image_part(source: dict) -> dict:
    media = source.get("media_type", "image/jpeg")
    return {"type": "input_image", "image_url": f"data:{media};base64,{source.get('data', '')}", "detail": "auto"}


def _assistant(content: list, replay_reasoning: bool) -> list:
    out = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text" and block["text"]:
            out.append({"role": "assistant", "content": block["text"]})
        elif block.get("type") == "tool_use":
            out.append({"type": "function_call", "call_id": block["id"], "name": block["name"],
                        "arguments": json.dumps(block["input"])})
        elif replay_reasoning and block.get("type") == "thinking" and block.get("id"):
            # A thinking block without an id never came from this wire; drop it.
            item = {"type": "reasoning", "id": block["id"],
                    "summary": [{"type": "summary_text", "text": block["thinking"]}] if block.get("thinking") else []}
            if block.get("signature"):
                item["encrypted_content"] = block["signature"]
            out.append(item)
    return out


def _user(content: list) -> list:
    out, parts = [], []
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "tool_result":
            tr = block.get("content", "")
            if isinstance(tr, list):
                # function_call_output takes a string; the image rides in the following user item.
                out.append({"type": "function_call_output", "call_id": block["tool_use_id"],
                            "output": "[image attached — see following message]"})
                parts += [_image_part(i.get("source", {})) for i in tr if i.get("type") == "image"]
            else:
                out.append({"type": "function_call_output", "call_id": block["tool_use_id"], "output": tr})
        elif btype == "text":
            parts.append({"type": "input_text", "text": block["text"]})
        elif btype == "image":
            parts.append(_image_part(block.get("source", {})))
        elif btype == "document":
            parts.append({"type": "input_text", "text": "[PDF document attached]"})
    return out + ([{"role": "user", "content": parts}] if parts else [])


def to_responses(anthropic_msgs: list, replay_reasoning=False, explicit_cache=False) -> tuple:
    """Anthropic-format transcript -> (instructions or None, Responses input items).
    With explicit_cache, cache_control/_ephemeral ride on items for _add_cache_breakpoints."""
    instructions, out = [], []
    for msg in anthropic_msgs:
        role = msg.get("role")
        content = msg.get("content", "")
        if role == "system":
            if not (isinstance(content, str) and content):
                continue
            if explicit_cache:
                # Top-level instructions can't carry a breakpoint, so system prompts become developer items.
                item = {"role": "developer", "content": content}
                if msg.get("cache_control"):
                    item["cache_control"] = msg["cache_control"]
                out.append(item)
            else:
                instructions.append(content)
        elif role == "assistant" and isinstance(content, list):
            out += _assistant(content, replay_reasoning)
        elif role == "user" and isinstance(content, list):
            out += _user(content)
        else:
            item = {"role": role, "content": content}
            if explicit_cache and msg.get("_ephemeral"):
                item["_ephemeral"] = True
            out.append(item)
    return ("\n\n".join(instructions) if instructions else None), out


def _taggable_field(msg: dict) -> str | None:
    """The field that can carry content blocks (and so a breakpoint); bare function_call items have none."""
    if msg.get("type") == "function_call_output":
        return "output"
    if "content" in msg:
        return "content"
    return None


def _tag_cache_breakpoint(msg: dict) -> dict:
    """Copy of `msg` with its last content block tagged prompt_cache_breakpoint."""
    field = _taggable_field(msg)
    if field is None:
        return msg
    tagged = dict(msg)
    value = tagged.get(field)
    if isinstance(value, str):
        tagged[field] = [{"type": "input_text", "text": value, "prompt_cache_breakpoint": {"mode": "explicit"}}]
    elif isinstance(value, list) and value:
        last = dict(value[-1]) if isinstance(value[-1], dict) else {}
        last["prompt_cache_breakpoint"] = {"mode": "explicit"}
        tagged[field] = [*value[:-1], last]
    return tagged


def _add_cache_breakpoints(items: list) -> list:
    """Tag every item the caller marked cache_control, plus a rolling breakpoint on the last stable
    taggable item (ephemeral tails skipped). Internal markers are stripped from every item."""
    if not items:
        return items
    explicit = {i for i, m in enumerate(items) if m.get("cache_control")}
    rolling = next((i for i in range(len(items) - 1, -1, -1)
                    if not items[i].get("_ephemeral") and _taggable_field(items[i]) is not None), None)
    out = []
    for i, m in enumerate(items):
        m = _tag_cache_breakpoint(m) if (i in explicit or i == rolling) else dict(m)
        m.pop("cache_control", None)
        m.pop("_ephemeral", None)
        out.append(m)
    return out


def client_for(provider: str, alias: str, timeout=480):
    base_url, api_key = credentials(provider)
    key = ("openai", provider, timeout, base_url, api_key)
    return cached_client(key, lambda: AsyncOpenAI(base_url=base_url, api_key=api_key, timeout=timeout))


async def call(provider, alias, model_id, *, input, encoded_image=None, tools=None, stream=False,
               thinking=False, reasoning_effort=None, max_tokens=None, client_timeout=480, pdf=None, retries=3):
    c = compat(provider)
    replay, cache = bool(c.get("replay_reasoning")), bool(c.get("explicit_cache"))
    client = client_for(provider, alias, client_timeout)

    instructions, transcript = None, False
    if pdf:
        resp_input = f"[PDF document attached]\n\n{input}"
    elif encoded_image:
        imgs = [encoded_image] if isinstance(encoded_image, str) else encoded_image
        parts = [{"type": "input_image", "image_url": f"data:image/jpeg;base64,{img}", "detail": "auto"} for img in imgs]
        resp_input = [{"role": "user", "content": [*parts, {"type": "input_text", "text": input}]}]
    elif isinstance(input, str):
        resp_input = input
    else:
        instructions, resp_input = to_responses(input, replay, cache)
        transcript = True
        if cache:
            resp_input = _add_cache_breakpoints(resp_input)

    params = {
        "model": model_id,
        "input": resp_input,
        "max_output_tokens": max_tokens or max_output_for(alias) or FALLBACK_MAX_TOKENS,
        "stream": stream,
        "reasoning": {"effort": resolve_effort(alias, reasoning_effort) or "high", "summary": "auto"},
    }
    if replay:
        # Without encrypted_content only the item id could be replayed, which ZDR can't resolve.
        params["include"] = ["reasoning.encrypted_content"]
    if instructions:
        params["instructions"] = instructions
    if cache and transcript:
        params["prompt_cache_options"] = {"mode": "explicit"}
    resp_tools = tools_to_responses(tools) if tools else None
    if resp_tools:
        params["tools"] = resp_tools
        params["tool_choice"] = "auto"

    return await retry_call(
        lambda: client.responses.create(**params),
        stream=stream,
        decode=responses.response,
        decode_stream=responses.stream_response,
        timeout=first_event_timeout(PROVIDERS.get(provider, {}).get("first_event_timeout", 120)),
        retries=retries,
        label=f"{provider} responses_call",
    )
