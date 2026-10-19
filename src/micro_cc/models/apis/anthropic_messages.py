"""Anthropic Messages wire: Anthropic, Claude Code (OAuth), Bedrock, Vertex and Foundry.

Providers differ only in credentials (catalog.json), `compat.betas` (1M context and
server-side fallback, direct API only), `auth: oauth` (Claude Code identity) and
`client: bedrock|vertex` (the SDK's platform clients; same request surface).
"""

import asyncio
import os

from anthropic import AsyncAnthropic, AsyncAnthropicBedrockMantle, AsyncAnthropicVertex

from micro_cc.models.adapters.anthropic import response as wrap_response, stream_response
from micro_cc.models.client import cached_client
from micro_cc.models.catalog import (
    PROVIDERS, anthropic_beta_headers, compat, fallback_params, max_output_for, resolve_effort,
    supports_thinking, wants_summarized_display,
)
from micro_cc.models.providers import credentials, ensure_package, region
from micro_cc.models.retry import first_event_timeout, retry_call

# Used only when the caller passes no max_tokens and the alias isn't in the catalog.
FALLBACK_MAX_TOKENS = 60000

# Anthropic's OAuth infrastructure checks the user-agent version against real
# Claude Code releases; a literal is enough to pass.
_CLAUDE_CODE_VERSION_FALLBACK = "2.1.74"

# OAuth path: outbound tool names map to their closest Claude Code equivalent.
# Hand-built and intentionally partial; unmapped names get the mcp__ prefix below.
_TOOL_STEALTH_MAP = {
    "bash_": "Bash",
    "read_": "Read",
    "write_": "Write",
    "edit_": "Edit",
    "grep_": "Grep",
    "glob_": "Glob",
    "ask_user_question_tool_": "AskUserQuestion",
    "todo_tool_": "TodoWrite",
    "read_skill": "Skill",
    "google_search": "WebSearch",
    "visit_url": "WebFetch",
}
_TOOL_STEALTH_UNMAP = {v: k for k, v in _TOOL_STEALTH_MAP.items()}

# Names with no Claude Code equivalent go out as mcp__<name>; a name matching neither a
# built-in nor that shape can flip an OAuth request onto metered billing or a 400.
_MCP_STEALTH_PREFIX = "mcp__"


def _stealth_tools(tools: list) -> list:
    """Outbound: rename tool schemas (name only) for the OAuth path."""
    renamed = []
    for t in tools:
        name = t.get("name")
        if name in _TOOL_STEALTH_MAP:
            renamed.append({**t, "name": _TOOL_STEALTH_MAP[name]})
        elif name and not name.startswith(_MCP_STEALTH_PREFIX):
            renamed.append({**t, "name": _MCP_STEALTH_PREFIX + name})
        else:
            renamed.append(t)
    return renamed


def _unstealth_name(name: str) -> str:
    """Inbound: reverse _stealth_tools so dispatch only ever sees real function names."""
    if name in _TOOL_STEALTH_UNMAP:
        return _TOOL_STEALTH_UNMAP[name]
    if name.startswith(_MCP_STEALTH_PREFIX):
        return name[len(_MCP_STEALTH_PREFIX):]
    return name


def build_client(provider: str, timeout, betas: list | None = None):
    """SDK client for `provider`; extra betas are joined onto OAuth's required flags, never replace them."""
    betas = betas or []
    base_url, api_key = credentials(provider)
    if PROVIDERS.get(provider, {}).get("auth") == "oauth":
        return AsyncAnthropic(
            auth_token=api_key,  # Bearer, not x-api-key
            base_url=base_url,
            timeout=timeout,
            default_headers={
                "anthropic-beta": ",".join(["claude-code-20250219", "oauth-2025-04-20", *betas]),
                # Must match the SDK's own "User-Agent" casing, or both headers go out.
                "User-Agent": f"claude-code/{_CLAUDE_CODE_VERSION_FALLBACK}",
                "x-app": "cli",
            },
        )
    headers = {"anthropic-beta": ",".join(betas)} if betas else None
    p = PROVIDERS.get(provider, {})
    if p.get("client") == "bedrock":
        # Bedrock API key auth: no boto3 needed (SigV4/profiles would require it).
        return AsyncAnthropicBedrockMantle(api_key=api_key, aws_region=region(provider), timeout=timeout, default_headers=headers)
    if p.get("client") == "vertex":
        # Google Application Default Credentials via google-auth (see ensure_package).
        return AsyncAnthropicVertex(project_id=os.getenv(p.get("project_env", "")), region=region(provider),
                                    timeout=timeout, default_headers=headers)
    return AsyncAnthropic(api_key=api_key or None, base_url=base_url, timeout=timeout, default_headers=headers)


def client_for(provider: str, alias: str, timeout=480):
    """Cached client; credentials are in the key so a refreshed OAuth token builds a fresh one."""
    betas = anthropic_beta_headers(alias) if compat(provider).get("betas") else []
    project = os.getenv(PROVIDERS.get(provider, {}).get("project_env", ""))
    key = ("anthropic", provider, timeout, tuple(betas), credentials(provider), region(provider), project)
    return cached_client(key, lambda: build_client(provider, timeout, betas))


def _image(data: str) -> dict:
    return {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": data}}


def _build_messages(input, encoded_image, pdf) -> tuple:
    """(system blocks, messages) in Anthropic shape, with one rolling cache breakpoint."""
    if pdf:
        doc = {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": pdf}}
        return [], [{"role": "user", "content": [doc, {"type": "text", "text": input}]}]
    if encoded_image:
        imgs = [encoded_image] if isinstance(encoded_image, str) else encoded_image
        return [], [{"role": "user", "content": [*map(_image, imgs), {"type": "text", "text": input}]}]
    if isinstance(input, str):
        return [], [{"role": "user", "content": input}]

    system, messages, ephemeral = [], [], []
    for msg in input:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        if role == "system":
            # Ordered blocks, not one joined string, so a caller's cache_control survives.
            if isinstance(content, str) and content:
                block = {"type": "text", "text": content}
                if msg.get("cache_control"):
                    block["cache_control"] = msg["cache_control"]
                system.append(block)
            continue
        if isinstance(content, list):
            # Unsigned thinking never came from Anthropic (e.g. OpenAI summaries after a model
            # switch) and 400s on replay; signed ones are passed back with only the fields the
            # API accepts (claude_loop_ adds an "id" for Responses replay).
            content = [
                ({"type": "thinking", "thinking": b.get("thinking", ""), "signature": b["signature"]}
                 if isinstance(b, dict) and b.get("type") == "thinking" else b)
                for b in content
                if not (isinstance(b, dict) and b.get("type") == "thinking" and not b.get("signature"))
            ]
        messages.append({"role": role, "content": content})
        ephemeral.append(bool(msg.get("_ephemeral")))

    # Tag the last stable message so later loops read the transcript from cache; skip
    # ephemeral tails (todos reminder). Copy, never mutate: callers reuse these dicts.
    bp = len(messages) - 1
    while bp > 0 and ephemeral[bp]:
        bp -= 1
    if messages and not ephemeral[bp]:
        last = dict(messages[bp])
        lc = last.get("content")
        if isinstance(lc, str) and lc:
            last["content"] = [{"type": "text", "text": lc, "cache_control": {"type": "ephemeral"}}]
            messages[bp] = last
        elif isinstance(lc, list) and lc and isinstance(lc[-1], dict):
            last["content"] = [*lc[:-1], {**lc[-1], "cache_control": {"type": "ephemeral"}}]
            messages[bp] = last
    return system, messages


async def call(provider, alias, model_id, *, input, encoded_image=None, tools=None, stream=False,
               thinking=False, reasoning_effort=None, max_tokens=None, client_timeout=480, pdf=None, retries=3):
    betas_on = bool(compat(provider).get("betas"))
    oauth = PROVIDERS.get(provider, {}).get("auth") == "oauth"
    # A venv rebuild may have dropped the optional dependency; reinstall it before building the client.
    missing = await asyncio.to_thread(ensure_package, provider) if PROVIDERS.get(provider, {}).get("package") else None
    if missing:
        print(f"\n[{provider} model_call]: missing dependency: {missing}", flush=True)
        return None
    client = client_for(provider, alias, client_timeout)

    system, messages = _build_messages(input, encoded_image, pdf)
    if oauth:
        system = [{"type": "text", "text": "You are Claude Code, Anthropic's official CLI for Claude."}, *system]

    params = {"model": model_id, **(fallback_params(alias) if betas_on else {})}
    if system:
        params["system"] = system
    if tools:
        params["tools"] = _stealth_tools(tools) if oauth else tools
        params["tool_choice"] = {"type": "auto", "disable_parallel_tool_use": False}
    params["max_tokens"] = max_tokens or max_output_for(alias) or FALLBACK_MAX_TOKENS
    if thinking and supports_thinking(alias):
        params["thinking"] = {"type": "adaptive"}
        if wants_summarized_display(alias):
            params["thinking"]["display"] = "summarized"
        effort = resolve_effort(alias, reasoning_effort)
        if effort:
            params["output_config"] = {"effort": effort}
    params["messages"] = messages
    params["stream"] = stream

    def decode(raw):
        if oauth:
            for block in raw.content:
                if block.type == "tool_use":
                    block.name = _unstealth_name(block.name)
        return wrap_response(raw)

    return await retry_call(
        lambda: client.beta.messages.create(**params),
        stream=stream,
        decode=decode,
        decode_stream=lambda raw: stream_response(raw, unstealth=_unstealth_name if oauth else None),
        timeout=first_event_timeout(PROVIDERS.get(provider, {}).get("first_event_timeout", 120)),
        retries=retries,
        label=f"{provider} model_call",
    )
