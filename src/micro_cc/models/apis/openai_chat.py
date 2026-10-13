"""OpenAI Chat Completions wire: LiteLLM, OpenRouter, Ollama and any compatible proxy.

`compat.reasoning` names the dialect: how thinking is requested, replayed and read back.
  litellm     Anthropic thinking params via extra_body, signed thinking_blocks replayed
  openrouter  unified `reasoning` param, plain `reasoning` text replayed
  ollama      `think` flag plus options.num_ctx from env, thinking never replayed
  read        nothing requested or replayed; reasoning_content/reasoning shown if returned
`compat.cache_control` forwards Anthropic cache breakpoints (LiteLLM -> Bedrock).
`compat.omit_max_tokens` lets the provider pick the output cap for models the catalog doesn't know.
"""

import json
import os

from openai import AsyncOpenAI

from micro_cc.models.adapters import chat
from micro_cc.models.catalog import (
    PROVIDERS, anthropic_beta_headers, compat, max_output_for, resolve_effort, supports_thinking,
    wants_summarized_display,
)
from micro_cc.models.providers import credentials
from micro_cc.models.retry import first_event_timeout, retry_call
from micro_cc.models.schema import tools_to_chat

FALLBACK_MAX_TOKENS = 120000
IMAGE_PLACEHOLDER = "[image attached — see following message]"


def _image_part(source: dict) -> dict:
    media = source.get("media_type", "image/jpeg")
    return {"type": "image_url", "image_url": {"url": f"data:{media};base64,{source.get('data', '')}"}}


def _assistant(content: list, dialect) -> dict:
    text, calls, thinking = [], [], []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            text.append(block["text"])
        elif block.get("type") == "tool_use":
            calls.append({
                "id": block["id"],
                "type": "function",
                "function": {"name": block["name"], "arguments": json.dumps(block["input"])},
            })
        elif block.get("type") == "thinking":
            thinking.append(block)
    m = {"role": "assistant", "content": "\n".join(text) if text else None}
    if calls:
        m["tool_calls"] = calls
    if thinking and dialect == "litellm":
        # LiteLLM requires signed thinking blocks resent on assistant messages.
        m["thinking_blocks"] = [
            {"type": "thinking", "thinking": b.get("thinking", ""), "signature": b.get("signature", "")} for b in thinking
        ]
    elif thinking and dialect == "openrouter" and thinking[-1].get("thinking"):
        # Best-effort replay in the same field OpenRouter returns it in; unsigned, so never load-bearing.
        m["reasoning"] = thinking[-1]["thinking"]
    return m


def _user(content: list) -> list:
    """Tool results become `tool` messages; text/images follow as one user message."""
    results, parts = [], []
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "tool_result":
            tr = block.get("content", "")
            if isinstance(tr, list):
                # `tool` messages don't take images everywhere; the image rides in the next user message.
                results.append({"role": "tool", "tool_call_id": block["tool_use_id"], "content": IMAGE_PLACEHOLDER})
                parts += [_image_part(i.get("source", {})) for i in tr if i.get("type") == "image"]
            else:
                results.append({"role": "tool", "tool_call_id": block["tool_use_id"], "content": tr})
        elif btype == "text":
            parts.append({"type": "text", "text": block["text"]})
        elif btype == "image":
            parts.append(_image_part(block.get("source", {})))
        elif btype == "document":
            parts.append({"type": "text", "text": "[PDF document attached]"})
    if len(parts) == 1 and parts[0]["type"] == "text":
        return results + [{"role": "user", "content": parts[0]["text"]}]
    return results + ([{"role": "user", "content": parts}] if parts else [])


def to_chat(anthropic_msgs: list, dialect=None, cache_control=False) -> list:
    """Anthropic-format transcript -> Chat Completions messages."""
    out = []
    for msg in anthropic_msgs:
        role = msg.get("role")
        content = msg.get("content", "")
        if role == "system":
            if cache_control and msg.get("cache_control") and isinstance(content, str):
                out.append({"role": "system", "content": [{"type": "text", "text": content, "cache_control": msg["cache_control"]}]})
            else:
                out.append({"role": "system", "content": content})
        elif role == "assistant" and isinstance(content, list):
            out.append(_assistant(content, dialect))
        elif role == "user" and isinstance(content, list):
            out += _user(content)
        else:
            m = {"role": role, "content": content}
            if msg.get("_ephemeral"):
                m["_ephemeral"] = True  # read by _tag_cache_breakpoint, stripped before sending
            out.append(m)
    return out


def _tag_cache_breakpoint(messages: list) -> None:
    """Rolling breakpoint on the last stable user/assistant message; `tool` messages need string
    content, so the walk-back skips them along with ephemeral tails. Replaces, never mutates, dicts."""
    bp = len(messages) - 1
    while bp > 0 and (messages[bp].get("_ephemeral") or messages[bp].get("role") == "tool"):
        bp -= 1
    if messages and not messages[bp].get("_ephemeral") and messages[bp].get("role") != "tool":
        last = dict(messages[bp])
        lc = last.get("content")
        if isinstance(lc, str) and lc:
            last["content"] = [{"type": "text", "text": lc, "cache_control": {"type": "ephemeral"}}]
            messages[bp] = last
        elif isinstance(lc, list) and lc and isinstance(lc[-1], dict):
            last["content"] = [*lc[:-1], {**lc[-1], "cache_control": {"type": "ephemeral"}}]
            messages[bp] = last


def _build_messages(input, encoded_image, pdf, dialect, cache_control) -> list:
    if pdf:
        return [{"role": "user", "content": f"[PDF document attached]\n\n{input}"}]
    if encoded_image:
        imgs = [encoded_image] if isinstance(encoded_image, str) else encoded_image
        parts = [{"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{img}"}} for img in imgs]
        return [{"role": "user", "content": [*parts, {"type": "text", "text": input}]}]
    if isinstance(input, str):
        return [{"role": "user", "content": input}]
    messages = to_chat(input, dialect, cache_control)
    if cache_control:
        _tag_cache_breakpoint(messages)
    for m in messages:
        m.pop("_ephemeral", None)
    return messages


def _thinking_body(dialect, alias, reasoning_effort) -> dict:
    """Dialect-specific extra_body for a thinking request."""
    if dialect == "litellm":
        body = {"thinking": {"type": "adaptive"}}
        effort = resolve_effort(alias, reasoning_effort)
        if effort:
            body["output_config"] = {"effort": effort}
        if wants_summarized_display(alias):
            body["thinking"]["display"] = "summarized"
        return body
    if dialect == "openrouter":
        return {"reasoning": {"effort": resolve_effort(alias, reasoning_effort) or "high"}}
    if dialect == "ollama":
        return {"think": True}
    return {}


async def call(provider, alias, model_id, *, input, encoded_image=None, tools=None, stream=False,
               thinking=False, reasoning_effort=None, max_tokens=None, client_timeout=480, pdf=None, retries=3):
    c = compat(provider)
    dialect = c.get("reasoning")
    base_url, api_key = credentials(provider)
    client = AsyncOpenAI(base_url=base_url, api_key=api_key, timeout=client_timeout)

    if dialect == "ollama":
        max_tokens = max_tokens or int(os.getenv("OLLAMA_MAX_OUTPUT") or 4096)
    max_tokens = max_tokens or max_output_for(alias) or (None if c.get("omit_max_tokens") else FALLBACK_MAX_TOKENS)
    params = {
        "model": model_id,
        "stream": stream,
        "messages": _build_messages(input, encoded_image, pdf, dialect, c.get("cache_control")),
    }
    if max_tokens:
        params["max_tokens"] = max_tokens
    if stream:
        params["stream_options"] = {"include_usage": True}

    # Non-standard params must ride in extra_body; the SDK rejects unknown top-level kwargs.
    extra = {}
    if dialect == "ollama":
        extra["options"] = {"num_ctx": int(os.getenv("OLLAMA_NUM_CTX") or 32768)}
    if thinking and (dialect == "ollama" or supports_thinking(alias)):
        extra.update(_thinking_body(dialect, alias, reasoning_effort))
    if c.get("betas"):
        # Bedrock takes anthropic-beta as a body field; server-side fallback is direct-API only.
        betas = anthropic_beta_headers(alias, server_fallback=False)
        if betas:
            extra["anthropic_beta"] = betas
    if extra:
        params["extra_body"] = extra

    chat_tools = tools_to_chat(tools) if tools else None
    if chat_tools:
        params["tools"] = chat_tools
        params["tool_choice"] = "auto"

    reasoning = dialect or "none"
    return await retry_call(
        lambda: client.chat.completions.create(**params),
        stream=stream,
        decode=lambda raw: chat.response(raw, reasoning=reasoning),
        decode_stream=lambda raw: chat.stream_response(raw, reasoning=reasoning),
        timeout=first_event_timeout(PROVIDERS.get(provider, {}).get("first_event_timeout", 180)),
        retries=retries,
        label=f"{provider} model_call",
    )
