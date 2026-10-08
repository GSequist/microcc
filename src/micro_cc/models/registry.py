"""Single source of truth for model aliases.

Every backend (anthropic.py = direct API, foundry.py = Anthropic-shaped API
behind a different base_url/key, litellm.py = proxy) used to keep its own
MODEL_MAP: alias -> backend-specific id, plus its own copy of the "does this
model support thinking / summarized display" conditionals. The two drifted —
e.g. msg_store_.py called litellm.py with a raw bedrock id that wasn't a key
in its MODEL_MAP, so it silently fell back to Sonnet instead of Haiku. Add a
model here once and every caller picks it up.

GPT-5.6 entries have no "anthropic" key — there's no direct-Anthropic-API
equivalent. They do have an "openai" key alongside "litellm": the litellm
id is "openai.gpt-5.6-*" (proxy routing prefix for its OpenAI/Azure
backend), and stripping that prefix is exactly the real OpenAI API model
id — so accounts with a plain OPENAI_API_KEY can reach the same models
directly via models/openai.py instead of through the proxy.
"""

import copy
import json
import os

_BUILTIN_MODELS = {
    # context_window / max_output are real API limits, used by
    # trim_budget_for() below to size token_cutter's history budget per
    # model instead of every model sharing one flat number regardless of
    # how much context it actually supports.
    #
    # For an "anthropic"-backed entry, context_window is a bare claim —
    # nothing here verifies it against what's actually unlocked on the
    # wire. Anthropic's real default ceiling per Claude model is 200_000;
    # a bigger window (e.g. the 1M-context beta some Claude models support)
    # only applies if the request also carries the matching anthropic-beta
    # flag: anthropic.py sends it as an HTTP header, litellm.py sends it as
    # an "anthropic_beta" extra_body field (Bedrock's Anthropic-compatible
    # surface is body-field-gated, not header-gated) — both keyed off
    # anthropic_beta_headers(alias) below, whenever context_window >
    # 200_000. That's what makes the 1_000_000 entries below real on those
    # two paths. Foundry is NOT covered (untouched, still assumes 200_000
    # worth of real room past whatever it's told) — its proxy's support for
    # this beta flag is unverified and it's unconfigured today (commented
    # out in .env) — so if it's ever wired back up for one of these
    # aliases, every budget derived from context_window (trim_budget_for,
    # the compaction trigger/fold cap, token_cutter's own trim ceiling)
    # would silently assume room that isn't real on THAT backend — not an
    # immediate error, just a transcript that keeps growing uncompacted
    # until it hits the true 200k wall and the API rejects it outright.
    # gpt-5.6's 1_050_000 below is unrelated — that's OpenAI's Responses API
    # (litellm/responses_api backend), a different provider with its own
    # real (non-beta-gated) window.
    "sonnet-5":   {"anthropic": "claude-sonnet-5",           "foundry": "claude-sonnet-5",           "litellm": "bedrock.anthropic.claude-sonnet-5",  "thinking": True,  "summarized_display": True,  "context_window": 1_000_000, "max_output": 128_000},
    # server_fallback: Sonnet 5.5 refuses in five classifier categories (cyber, bio,
    # frontier_llm, reasoning_extraction, general_harms); same "default" fallback as
    # opus-5.5, direct Anthropic API only. micro-cc only sends thinking when the toggle is
    # on and then always {type: "adaptive"}, so Sonnet 5.5's 400 on {type: "disabled"}
    # is never hit. Foundry/Bedrock ids follow the sonnet-5 pattern (both list
    # claude-sonnet-5-5 as available at launch).
    "sonnet-5.5": {"anthropic": "claude-sonnet-5-5",         "foundry": "claude-sonnet-5-5",         "litellm": "bedrock.anthropic.claude-sonnet-5-5", "thinking": True,  "summarized_display": True,  "context_window": 1_000_000, "max_output": 128_000, "server_fallback": True},
    "opus-5":     {"anthropic": "claude-opus-5",            "foundry": "claude-opus-5",             "litellm": "bedrock.anthropic.claude-opus-5",    "thinking": True,  "summarized_display": True,  "context_window": 1_000_000, "max_output": 128_000},
    # server_fallback: broader safety classifiers than opus-5 (bio/reasoning_extraction
    # join cyber) mean legitimate research turns can trip stop_reason: "refusal" —
    # confirmed live 2026-09-22. Server-side fallback retries a
    # refused request against Anthropic's recommended fallback model inside the same
    # API call. claude_loop_.py's refusal check is the backstop for reasoning_extraction,
    # which fallback won't retry. Only anthropic.py's fallback_params() reads this key.
    "opus-5.5":   {"anthropic": "claude-opus-5-5",           "foundry": "claude-opus-5-5",           "litellm": "bedrock.anthropic.claude-opus-5-5",  "thinking": True,  "summarized_display": True,  "context_window": 1_000_000, "max_output": 128_000, "server_fallback": True},
    "opus-4.8":   {"anthropic": "claude-opus-4-8",           "foundry": "claude-opus-4-8",           "litellm": "bedrock.anthropic.claude-opus-4-8",  "thinking": True,  "summarized_display": True,  "context_window": 1_000_000, "max_output": 128_000},
    "opus-4.7":   {"anthropic": "claude-opus-4-7",           "foundry": "claude-opus-4-7",           "litellm": "bedrock.anthropic.claude-opus-4-7",  "thinking": True,  "summarized_display": True,  "context_window": 1_000_000, "max_output": 128_000},
    "opus-4.6":   {"anthropic": "claude-opus-4-6",           "foundry": "claude-opus-4-6",           "litellm": "bedrock.anthropic.claude-opus-4-6",  "thinking": True,  "summarized_display": False, "context_window": 1_000_000, "max_output": 128_000},
    "sonnet-4.6": {"anthropic": "claude-sonnet-4-6",         "foundry": "claude-sonnet-4-6",         "litellm": "bedrock.anthropic.claude-sonnet-4-6", "thinking": True,  "summarized_display": False, "context_window": 1_000_000, "max_output": 128_000},
    "haiku-5.5":  {"anthropic": "claude-haiku-5-5",          "foundry": "claude-haiku-5-5",          "litellm": "bedrock.anthropic.claude-haiku-5-5", "thinking": True,  "summarized_display": True,  "context_window": 1_000_000, "max_output": 128_000},
    "gpt-5.6-terra": {"litellm": "openai.gpt-5.6-terra", "openai": "gpt-5.6-terra", "thinking": True, "summarized_display": False, "responses_api": True, "context_window": 1_050_000, "max_output": 128_000},
    "gpt-5.6-luna":  {"litellm": "openai.gpt-5.6-luna",  "openai": "gpt-5.6-luna",  "thinking": True, "summarized_display": False, "responses_api": True, "context_window": 1_050_000, "max_output": 128_000},
    "gpt-5.6-sol":   {"litellm": "openai.gpt-5.6-sol",   "openai": "gpt-5.6-sol",   "thinking": True, "summarized_display": False, "responses_api": True, "context_window": 1_050_000, "max_output": 128_000},
    "gpt-6-astra":   {"litellm": "openai.gpt-6-astra",   "openai": "gpt-6-astra",   "thinking": True, "summarized_display": False, "responses_api": True, "context_window": 1_050_000, "max_output": 128_000},
    "gpt-6-sol":     {"litellm": "openai.gpt-6-sol",     "openai": "gpt-6-sol",     "thinking": True, "summarized_display": False, "responses_api": True, "context_window": 1_050_000, "max_output": 128_000},
    "gpt-6-luna":    {"litellm": "openai.gpt-6-luna",    "openai": "gpt-6-luna",    "thinking": True, "summarized_display": False, "responses_api": True, "context_window": 1_050_000, "max_output": 128_000},
}

_BUILTIN_DEFAULT = "sonnet-5"

# Transcript trim budget (token_cutter's max_tokens in claude_loop_.py) —
# how much of the conversation history stays in context before older turns
# get cut. Floor/fallback only now — see trim_budget_for() below for the
# per-model value every registered alias actually gets. Stays low
# deliberately: it's what an unrecognized/unregistered alias falls back to,
# not a ceiling on any real model.
DEFAULT_TRIM_BUDGET = 100_000

# ---------------------------------------------------------------------------
# Context-compaction budget — ONE rule for checkpoint-style compaction.
# Given a model's real published context_window and max_output:
#   B (trim_budget_for)     = context_window - max_output - PROMPT_RESERVE
#     the hard ceiling token_cutter trims transcript history to, and the
#     basis for both numbers below.
#   trigger (compaction_trigger_for) = int(TRIGGER_RATIO * B)
#     compact once the backlog since the last checkpoint (rows after
#     as_of_index, NOT counting the checkpoint summary itself) reaches this
#     many tokens.
#   fold budget (at the compaction call site, not a registry helper here)
#     = B - tokens(previous summary): one summarizer call takes the previous
#     summary + the whole backlog, capped at this; if the backlog is bigger,
#     fold only what fits and advance as_of_index only that far (at least
#     one row always folds), leaving the rest for the next pass.
#   summary cap (summary_cap_for) = min(int(SUMMARY_CAP_FRACTION * B),
#     int(max_output * 0.9)) — bounds the summary itself, since every later
#     pass builds on top of it and a tight cap would permanently squeeze out
#     older detail.
#   failure: if summarization fails/returns nothing, as_of_index must NOT
#     advance — the caller retries the same backlog range next time, nothing
#     is silently dropped.
PROMPT_RESERVE = 20_000
TRIGGER_RATIO = 0.8
SUMMARY_CAP_FRACTION = 0.10

# Per-backend fallback for trim_budget_for when `alias` isn't in MODELS —
# OpenRouter aliases are free-text (never registered), so this is what they
# always get instead of the conservative DEFAULT_TRIM_BUDGET. Ollama isn't
# here: it derives its own budget from OLLAMA_NUM_CTX (start_live_tui_.py's
# _ollama_trim_budget) and never calls this function.
BACKEND_DEFAULT_TRIM_BUDGET = {
    "openrouter": 1_000_000,
}


def trim_budget_for(alias: str, backend: str | None = None) -> int:
    """Per-model trim budget: context_window - max_output - PROMPT_RESERVE,
    floored at DEFAULT_TRIM_BUDGET or BACKEND_DEFAULT_TRIM_BUDGET[backend]."""
    entry = _entry(alias)
    window = entry.get("context_window") if entry else None
    if not window:
        return BACKEND_DEFAULT_TRIM_BUDGET.get(backend, DEFAULT_TRIM_BUDGET)
    max_output = entry.get("max_output", 0) if entry else 0
    return max(DEFAULT_TRIM_BUDGET, window - max_output - PROMPT_RESERVE)


def _entry(alias: str):
    """Per-repo adapter: this registry's model dict. The only line of the
    budget helpers below that differs between repos."""
    return MODELS.get(alias)


def max_output_for(alias: str) -> int | None:
    """Registered max_output for `alias`, or None if unknown."""
    entry = _entry(alias)
    return entry.get("max_output") if entry else None


def compaction_trigger_for(alias: str, budget: int | None = None) -> int:
    """Backlog size (tokens since the last checkpoint) that triggers a fold:
    TRIGGER_RATIO x B. `budget` overrides B (micro-cc passes its
    Ollama-aware effective budget)."""
    return int(TRIGGER_RATIO * (budget or trim_budget_for(alias)))


def summary_cap_for(alias: str, budget: int | None = None) -> int:
    """Max size of the checkpoint summary: SUMMARY_CAP_FRACTION x B, never
    above 90% of the summarizer's own max_output. `budget` overrides B."""
    cap = int(SUMMARY_CAP_FRACTION * (budget or trim_budget_for(alias)))
    max_output = max_output_for(alias)
    if max_output:
        cap = min(cap, int(max_output * 0.9))
    return cap


# Aliases shown in the /model picker UI, in display order.
_BUILTIN_OPTIONS = [
    "sonnet-5", "sonnet-5.5", "opus-5", "opus-5.5", "opus-4.8", "opus-4.7", "opus-4.6", "sonnet-4.6", "haiku-5.5",
    "gpt-5.6-terra", "gpt-5.6-luna", "gpt-5.6-sol", "gpt-6-astra", "gpt-6-sol", "gpt-6-luna",
]


# ---------------------------------------------------------------------------
# Catalog layers: builtin (above, offline fallback) < remote cache of the
# GitHub models.json (catalog_.py refreshes it) < user ~/.micro-cc/models.json.
# Built once at import from local files only (no network); a refresh applies
# on the next start.
_BACKENDS = ("anthropic", "foundry", "litellm", "openai")
_BOOLS = ("thinking", "summarized_display", "responses_api", "server_fallback")
_INTS = ("context_window", "max_output")
_REQUIRED = ("thinking", "summarized_display", "context_window", "max_output")


def remote_cache_path() -> str:
    return os.path.join(os.path.expanduser("~"), ".micro-cc", "cache", "models.json")


def user_catalog_path() -> str:
    return os.path.join(os.path.expanduser("~"), ".micro-cc", "models.json")


def _running_version() -> tuple:
    try:
        from importlib.metadata import version
        return _parse_version(version("micro-cc"))
    except Exception:
        return (0, 0, 0)


def _parse_version(v) -> tuple:
    parts = tuple(int(x) for x in v.split("."))
    if len(parts) != 3:
        raise ValueError(v)
    return parts


def _check_entry(e) -> dict:
    """Validated fields of one entry (unknown keys dropped); raises ValueError."""
    if not isinstance(e, dict):
        raise ValueError("not an object")
    out = {}
    for k, v in e.items():
        if k in _BACKENDS:
            if not isinstance(v, str) or not v:
                raise ValueError(f"{k} must be a non-empty string")
        elif k in _BOOLS:
            if not isinstance(v, bool):
                raise ValueError(f"{k} must be true/false")
        elif k in _INTS:
            if isinstance(v, bool) or not isinstance(v, int) or v <= 0:
                raise ValueError(f"{k} must be a positive integer")
        elif k == "min_version":
            if not isinstance(v, str):
                raise ValueError("min_version must be a string")
            if _parse_version(v) > _running_version():
                raise ValueError(f"needs micro-cc >= {v}")
            continue
        else:
            continue
        out[k] = v
    return out


def validate_catalog(doc) -> tuple:
    """(entries, default_model, skipped) from a catalog document; bad entries are skipped, not the file."""
    if not isinstance(doc, dict) or not isinstance(doc.get("models", {}), dict):
        return {}, None, ["malformed document"]
    entries, skipped = {}, []
    for alias, e in doc.get("models", {}).items():
        try:
            entries[alias] = _check_entry(e)
        except ValueError as err:
            skipped.append(f"{alias}: {err}")
    default = doc.get("default_model")
    return entries, default if isinstance(default, str) else None, skipped


def _read_layer(path: str):
    """(doc, status): status is 'absent', 'ok' or 'unreadable: ...'."""
    try:
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
    except FileNotFoundError:
        return None, "absent"
    except Exception as e:
        return None, f"unreadable: {type(e).__name__}"
    if not isinstance(doc, dict):
        return None, "unreadable: not an object"
    return doc, "ok"


def build_catalog() -> tuple:
    """(models, options, default, status) merged builtin < remote cache < user file."""
    models = copy.deepcopy(_BUILTIN_MODELS)
    options = list(_BUILTIN_OPTIONS)
    default = _BUILTIN_DEFAULT
    status = {"skipped": []}
    for name, path in (("remote", remote_cache_path()), ("user", user_catalog_path())):
        doc, status[name] = _read_layer(path)
        if doc is None:
            continue
        entries, layer_default, skipped = validate_catalog(doc)
        status["skipped"] += [f"{name} {s}" for s in skipped]
        for alias, fields in entries.items():
            if alias in models:
                models[alias].update(fields)
            elif any(b in fields for b in _BACKENDS) and all(k in fields for k in _REQUIRED):
                models[alias] = fields
                options.append(alias)
            else:
                status["skipped"].append(f"{name} {alias}: new alias needs a backend id and {', '.join(_REQUIRED)}")
        if layer_default in models:
            default = layer_default
    return models, options, default, status


MODELS, MODEL_OPTIONS, DEFAULT_MODEL, CATALOG_STATUS = build_catalog()

# Bare family names resolve to the same entry as the versioned alias (never in MODEL_OPTIONS).
for _short, _alias in (("haiku", "haiku-5.5"), ("sonnet", "sonnet-5"), ("opus", "opus-5")):
    MODELS[_short] = MODELS[_alias]



def options_for_backend(backend: str) -> list:
    """MODEL_OPTIONS filtered to aliases actually reachable on `backend`
    ('anthropic', 'foundry', 'litellm', or 'openai'). Anthropic-direct/
    Foundry have no route to the gpt-5.6-* family, and openai-direct has no
    route to the Claude family (see module docstring) — the /login model
    picker must filter these out after a given provider's login, otherwise
    picking an unreachable one 400s outright instead of just not being
    offered.
    """
    return [alias for alias in MODEL_OPTIONS if backend in MODELS[alias]]


def resolve(alias: str, backend: str) -> str:
    """Alias -> backend-specific model id ('anthropic' or 'litellm').

    Unknown aliases pass through unchanged rather than silently substituting
    the default model — a typo'd or not-yet-registered name should surface
    as an API error, not silently run (and bill) a different model. Aliases
    with no entry for the requested backend (e.g. a gpt-5.6-* model asked
    for on 'anthropic') also pass through unchanged, so the direct Anthropic
    API call fails loudly instead of silently downgrading to Sonnet.
    """
    entry = MODELS.get(alias)
    if entry is None:
        return alias
    return entry.get(backend, alias)


def supports_thinking(alias: str) -> bool:
    entry = MODELS.get(alias)
    return entry["thinking"] if entry else True


def wants_summarized_display(alias: str) -> bool:
    entry = MODELS.get(alias)
    return entry["summarized_display"] if entry else False


def anthropic_beta_headers(alias: str, server_fallback: bool = True) -> list:
    """anthropic-beta flags anthropic.py must send for `alias`'s
    context_window claim to be real on the direct-Anthropic wire (see the
    warning above MODELS) — "context-1m-2025-08-07", only past the real
    200_000 default ceiling — plus the server-side-fallback beta for
    aliases flagged "server_fallback" when `server_fallback` (direct-Anthropic
    only; must travel with fallback_params(), litellm passes False)."""
    entry = MODELS.get(alias)
    betas = []
    window = entry.get("context_window") if entry else None
    if window and window > 200_000:
        betas.append("context-1m-2025-08-07")
    if server_fallback and entry and entry.get("server_fallback"):
        betas.append("server-side-fallback-2026-07-01")
    return betas


def fallback_params(alias: str) -> dict:
    """Server-side fallback request params for `alias` on the direct-
    Anthropic wire, or {} if it doesn't want one. Anthropic's recommended
    fallback model per refusal category — keeps a refused request from
    ever reaching the user as a dead end, resolved inside the same API
    call. Must travel with the beta flag anthropic_beta_headers() adds
    above. litellm/foundry/openai backends don't read this — server-side
    fallback is a direct-Anthropic-API feature."""
    entry = MODELS.get(alias)
    if entry and entry.get("server_fallback"):
        return {"fallbacks": "default"}
    return {}


def uses_responses_api(alias: str) -> bool:
    """gpt-5.6-* on the LiteLLM proxy reject function tools on
    /v1/chat/completions while reasoning is active ("Function tools with
    reasoning_effort are not supported ... use /v1/responses"). These models
    must go through client.responses.create instead, which supports
    reasoning + tools together."""
    entry = MODELS.get(alias)
    return entry.get("responses_api", False) if entry else False
