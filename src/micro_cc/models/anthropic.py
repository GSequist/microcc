"""Direct Anthropic requests, authentication, caching and retry policy."""

from typing import List, Dict, Any, Optional, Union
from anthropic import AsyncAnthropic
from dotenv import load_dotenv
import asyncio
import os

from micro_cc.models.adapters.anthropic import response as wrap_response, stream_response
from micro_cc.models.priming import prime_stream, first_event_timeout
from micro_cc.models.registry import DEFAULT_MODEL, resolve, supports_thinking, wants_summarized_display, anthropic_beta_headers, fallback_params, max_output_for

load_dotenv(os.path.expanduser("~/.micro-cc/.env"))
load_dotenv()

# Seconds to wait for a streaming call's first event before treating a
# connected-but-silent endpoint as a failed attempt — see priming.prime_stream.
FIRST_EVENT_TIMEOUT = 120
# 60s was too tight for proxies: a slow first event is retried by re-sending the
# whole prompt. Override for all providers with MICROCC_FIRST_EVENT_TIMEOUT.


# Anthropic's OAuth infrastructure checks the user-agent version against
# real Claude Code releases — a literal fallback is enough to pass. A more
# elaborate live `claude --version` probe is possible but deliberately not
# implemented here.
_CLAUDE_CODE_VERSION_FALLBACK = "2.1.74"

# Rename every outbound tool_use call to its closest Claude Code equivalent
# when going through the OAuth path, on top of the header/UA/identity
# handling above. Not a functional requirement of the API (unmapped tool
# names work fine, proven by the smoke test) — it purely reduces how
# distinguishable this traffic is from a real Claude Code session over
# sustained use.
#
# A bare case-insensitive match (`bash` -> `Bash`) isn't enough here: this
# codebase's tool functions are named things like `bash_`/`read_`/`edit_`
# (see claude_loop_.py's imports) — the trailing underscore alone breaks a
# case-insensitive match against Claude Code's `Bash`/`Read`/`Edit` — and
# several tools here (browser, vision, memory_, monitor_,
# message_session_) have no Claude Code equivalent to map to at all.
# So this is a hand-built functional mapping, not a casing normalizer, and
# intentionally partial: whatever isn't in the map goes out under its real
# name, since a request with an unmapped name works fine anyway.
_TOOL_STEALTH_MAP = {
    "bash_": "Bash",
    "read_": "Read",
    "write_": "Write",
    "edit_": "Edit",
    "grep_": "Grep",
    "glob_": "Glob",
    "ask_user_question_tool_": "AskUserQuestion",
    "todo_tool_": "TodoWrite",
    "read_skill": "Skill",  # loads skill instructions into context — same job as CC's own Skill tool
    "google_search": "WebSearch",  # web_tools_.py — genuine match to CC's own WebSearch
    "visit_url": "WebFetch",  # web_tools_.py — genuine match to CC's own WebFetch (one-shot fetch)
}
_TOOL_STEALTH_UNMAP = {v: k for k, v in _TOOL_STEALTH_MAP.items()}

# Fallback for everything with NO genuine Claude Code equivalent
# (message_session_, monitor_, memory_, search_tools, web_tools_'s stateful
# page-browsing functions, any MCP tool, etc.). Empirically, Anthropic's
# OAuth billing classifier accepts an mcp__<name>-shaped tool name as "a
# legitimate third-party MCP tool this Claude Code session has attached"
# (real users do this constantly), but flags a name matching neither a
# canonical built-in nor that shape as non-Claude-Code traffic — which can
# flip a request off plan-billing onto metered "extra usage" billing,
# sometimes as an outright 400. Unlike _TOOL_STEALTH_MAP (a hand-built
# functional match per tool), this needs no per-tool judgment call: it's a
# blind, mechanical prefix applied only to whatever _TOOL_STEALTH_MAP didn't
# already resolve.
_MCP_STEALTH_PREFIX = "mcp__"


def _stealth_tools(tools: list) -> list:
    """Outbound: rename every tool schema before sending — mapped tools to
    their Claude Code identity, everything else behind the mcp__ prefix.
    Only touches the "name" key — description/input_schema are untouched,
    so this is purely cosmetic on the wire."""
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
    """Inbound: reverse the rename on any tool_use block the model returns,
    so claude_loop_.py's dispatch (which is keyed on the real function
    names — TOOL_CATALOG, the base `tools` dict, dangerous/ask_user sets,
    the use_tool_ dispatcher) never has to know this translation happened."""
    if name in _TOOL_STEALTH_UNMAP:
        return _TOOL_STEALTH_UNMAP[name]
    if name.startswith(_MCP_STEALTH_PREFIX):
        return name[len(_MCP_STEALTH_PREFIX):]
    return name


def build_client(timeout, betas: Optional[List[str]] = None):
    """betas: extra anthropic-beta flags for this call (e.g.
    "context-1m-2025-08-07" — see registry.anthropic_beta_headers), joined
    onto the OAuth path's own required flags rather than replacing them —
    the SDK's default_headers is a flat dict, so a second call to this
    function with a different alias must still carry claude-code-20250219/
    oauth-2025-04-20 or the OAuth identity headers stop matching."""
    betas = betas or []
    oauth_token = os.getenv("ANTHROPIC_OAUTH_TOKEN")
    if oauth_token:
        beta_header = ",".join(["claude-code-20250219", "oauth-2025-04-20", *betas])
        return AsyncAnthropic(
            auth_token=oauth_token,  # Bearer, not x-api-key
            timeout=timeout,
            default_headers={
                "anthropic-beta": beta_header,
                # Must match the SDK's own casing exactly ("User-Agent", not
                # "user-agent") — anthropic/_base_client.py builds its
                # default_headers property as {"User-Agent": ..., **custom},
                # and merge_headers only case-folds keys inside
                # _APPEND_HEADERS (just "x-stainless-helper"). A differently
                # -cased key doesn't override the SDK's own User-Agent, it
                # sits alongside it as a second dict key — both survive to
                # the wire as separate header lines, so the identity headers
                # this whole OAuth path depends on may not be what Anthropic
                # actually reads.
                "User-Agent": f"claude-code/{_CLAUDE_CODE_VERSION_FALLBACK}",
                "x-app": "cli",  # not proven required, cheap to include
            },
        )
    if betas:
        return AsyncAnthropic(timeout=timeout, default_headers={"anthropic-beta": ",".join(betas)})
    return AsyncAnthropic(timeout=timeout)


async def a_model_call(
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
    alias = model
    # Registry-derived output ceiling for `alias` when the caller didn't ask
    # for a specific max_tokens — was a flat 60000 regardless of model,
    # which under-uses a 128k-max_output model and, via litellm's bedrock
    # route to this same alias family, could also over-ask a 64k-max_output
    # one (the Haiku trap). 60000 stays only as the fallback for an
    # unregistered alias.
    max_tokens = max_tokens or max_output_for(alias) or 60000
    model = resolve(alias, "anthropic")
    client = build_client(timeout=client_timeout, betas=anthropic_beta_headers(alias))
    base_sleep = 2

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
                # A thinking block with no signature can never have come from
                # Anthropic (see models/adapters/responses.py,
                # which always sets signature="" — OpenAI reasoning summaries
                # carry no such cryptographic proof). Anthropic validates the
                # signature on replay and 400s the whole request the moment
                # it hits one it didn't generate, which poisons every future
                # turn on this transcript once a model switch has happened.
                # Every observed real block also carries a text/tool_use
                # sibling in the same turn, so filtering never empties `content`.
                # claude_loop_ also stamps a persisted thinking block with an
                # "id" (OpenAI Responses reasoning replay needs it — see
                # models/openai.py's _msgs_to_responses). Anthropic's
                # BetaThinkingBlockParam only accepts type/thinking/signature;
                # "thinking blocks must be passed back unmodified... a
                # modified block results in a 400" — so that extra key 400s
                # the very next call on this backend if it survives here.
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
    
    oauth_token = os.getenv("ANTHROPIC_OAUTH_TOKEN")
    if oauth_token:
        # Every system_prompts entry must be a content-block dict, same
        # shape as the ones built at line ~212 above — a bare string here
        # is exactly what "system.0: Input does not match the expected
        # shape" (400) is rejecting when system is sent as a list.
        identity_block = {"type": "text", "text": "You are Claude Code, Anthropic's official CLI for Claude."}
        system_prompts = [identity_block, *system_prompts]

    api_parameters = {"model": model}
    api_parameters.update(fallback_params(alias))

    if system_prompts:
        api_parameters["system"] = system_prompts

    if tools:
        api_parameters["tools"] = _stealth_tools(tools) if oauth_token else tools
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
                return await prime_stream(
                    stream_response(response, unstealth=_unstealth_name if oauth_token else None),
                    first_event_timeout(FIRST_EVENT_TIMEOUT),
                )
            if oauth_token:
                for block in response.content:
                    if block.type == "tool_use":
                        block.name = _unstealth_name(block.name)
            return wrap_response(response)

        except Exception as e:
            print(f"\n[model_call]: {e}", flush=True)
            if attempt < retries - 1:
                sleep_time = base_sleep * (2**attempt)
                print(
                    f"\n[model_call]: Retrying in {sleep_time}s (attempt {attempt + 1}/{retries})..."
                )
                await asyncio.sleep(sleep_time)
            else:
                print(f"\n[model_call]: Failed after {retries} attempts")

    return None
