"""Model catalog: aliases, backend ids, capabilities, limits, providers and login prompts.

The data lives in models.json (the same file the website reads). It is merged from three layers,
later layers override earlier ones key by key, once at import and without network access:
  1. models.json shipped in this package - the offline fallback
  2. ~/.micro-cc/cache/models.json - GitHub's copy, refreshed in the background by catalog_.py
  3. ~/.micro-cc/models.json - the user's own overrides and extra models
A missing or broken layer is skipped; a bad entry is skipped and reported in CATALOG_STATUS["skipped"].
"""

import json
import os

PACKAGED_PATH = os.path.join(os.path.dirname(__file__), "models.json")

_BACKENDS = ("anthropic", "foundry", "litellm", "openai")
_BOOLS = ("thinking", "summarized_display", "responses_api", "server_fallback")
_INTS = ("context_window", "max_output")
_REQUIRED = ("thinking", "summarized_display", "context_window", "max_output")


# --- layers -----------------------------------------------------------------

def remote_cache_path() -> str:
    return os.path.join(os.path.expanduser("~"), ".micro-cc", "cache", "models.json")


def user_catalog_path() -> str:
    return os.path.join(os.path.expanduser("~"), ".micro-cc", "models.json")


def _parse_version(v) -> tuple:
    parts = tuple(int(x) for x in v.split("."))
    if len(parts) != 3:
        raise ValueError(v)
    return parts


def _running_version() -> tuple:
    try:
        from importlib.metadata import version
        return _parse_version(version("micro-cc"))
    except Exception:
        return (0, 0, 0)


def _read_layer(path: str):
    """(doc, status); status is 'absent', 'ok' or 'unreadable: <reason>' and only feeds CATALOG_STATUS."""
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


def _read_layers(status: dict) -> list:
    """[(layer name, doc)] for the layers that loaded, packaged first. Packaged must load."""
    packaged, packaged_status = _read_layer(PACKAGED_PATH)
    if packaged is None:
        raise RuntimeError(f"micro-cc is missing its shipped models.json ({packaged_status})")
    layers = [("packaged", packaged)]
    for name, path in (("remote", remote_cache_path()), ("user", user_catalog_path())):
        doc, status[name] = _read_layer(path)
        if doc is not None:
            layers.append((name, doc))
    return layers


# --- validation -------------------------------------------------------------

def _check_entry(e) -> dict:
    """Validated fields of one model entry (unknown keys dropped); raises ValueError."""
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
        elif k == "effort_levels":
            if not isinstance(v, list) or not all(isinstance(x, str) and x for x in v):
                raise ValueError("effort_levels must be a list of strings")
        elif k == "default_effort":
            if v is not None and not isinstance(v, str):
                raise ValueError("default_effort must be a string or null")
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
    """(entries, default_model, skipped) from one document; bad entries are skipped, not the file."""
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


def _check_provider(p) -> dict:
    """Valid fields of one provider: backend (str), login (list of env var names), oauth/free_text_model (bool)."""
    if not isinstance(p, dict):
        return {}
    out = {}
    if isinstance(p.get("backend"), str):
        out["backend"] = p["backend"]
    if isinstance(p.get("login"), list) and all(isinstance(x, str) for x in p["login"]):
        out["login"] = p["login"]
    for k in ("oauth", "free_text_model"):
        if isinstance(p.get(k), bool):
            out[k] = p[k]
    return out


# --- merging ----------------------------------------------------------------

def build_catalog() -> tuple:
    """(models, options, default, status): every layer merged, options in file order."""
    status = {"skipped": []}
    models, options, default = {}, [], None
    for name, doc in _read_layers(status):
        entries, layer_default, skipped = validate_catalog(doc)
        status["skipped"] += [f"{name} {s}" for s in skipped]
        for alias, fields in entries.items():
            if alias in models:
                models[alias].update(fields)  # override only the fields this layer sets
            elif any(b in fields for b in _BACKENDS) and all(k in fields for k in _REQUIRED):
                models[alias] = dict(fields)
                options.append(alias)
            else:
                status["skipped"].append(f"{name} {alias}: new alias needs a backend id and {', '.join(_REQUIRED)}")
        if layer_default in models:
            default = layer_default
    return models, options, default, status


def build_providers() -> tuple:
    """(providers, login_prompts) merged across layers the same way."""
    providers, prompts = {}, {}
    for _name, doc in _read_layers({}):
        for pname, p in (doc.get("providers") if isinstance(doc.get("providers"), dict) else {}).items():
            providers.setdefault(pname, {}).update(_check_provider(p))
        if isinstance(doc.get("login_prompts"), dict):
            prompts.update({k: v for k, v in doc["login_prompts"].items() if isinstance(k, str) and isinstance(v, str)})
    return providers, prompts


# What shipped with this version; tests compare layers against it.
_PACKAGED_DOC = _read_layer(PACKAGED_PATH)[0] or {}
_BUILTIN_MODELS, _BUILTIN_DEFAULT, _ = validate_catalog(_PACKAGED_DOC)
_BUILTIN_OPTIONS = list(_BUILTIN_MODELS)

MODELS, MODEL_OPTIONS, DEFAULT_MODEL, CATALOG_STATUS = build_catalog()
PROVIDERS, LOGIN_PROMPTS = build_providers()
MODEL_PROVIDERS = list(PROVIDERS)
LOGIN_SEQUENCE = {n: p["login"] for n, p in PROVIDERS.items() if p.get("login")}


# --- model lookups ----------------------------------------------------------

def options_for_backend(backend: str) -> list:
    """Aliases that have an id on `backend`; a model with none there would 400 if picked."""
    return [alias for alias in MODEL_OPTIONS if backend in MODELS[alias]]


def options_for_endpoint(endpoint: str, current_model: str | None) -> list:
    """Aliases the endpoint can serve; free-text providers (Ollama, OpenRouter) only offer the current model."""
    provider = PROVIDERS.get(endpoint, {})
    if provider.get("free_text_model"):
        return [current_model] if current_model else []
    return options_for_backend(provider.get("backend", "litellm"))


def resolve(alias: str, backend: str) -> str:
    """Alias -> the backend's model id. Unknown aliases and aliases without a `backend` id pass through
    unchanged, so a typo fails at the API instead of silently running another model."""
    entry = MODELS.get(alias)
    return entry.get(backend, alias) if entry else alias


def supports_thinking(alias: str) -> bool:
    entry = MODELS.get(alias)
    return entry["thinking"] if entry else True


def wants_summarized_display(alias: str) -> bool:
    entry = MODELS.get(alias)
    return entry["summarized_display"] if entry else False


def uses_responses_api(alias: str) -> bool:
    """True when litellm should call `alias` through the Responses API instead of chat completions."""
    entry = MODELS.get(alias)
    return entry.get("responses_api", False) if entry else False


def effort_levels_for(alias: str) -> list:
    entry = MODELS.get(alias)
    return list(entry.get("effort_levels", [])) if entry else []


def default_effort_for(alias: str):
    entry = MODELS.get(alias)
    return entry.get("default_effort") if entry else None


def resolve_effort(alias: str, requested):
    """`requested` if the model supports it, else the model's own default."""
    return requested if requested in effort_levels_for(alias) else default_effort_for(alias)


def anthropic_beta_headers(alias: str, server_fallback: bool = True) -> list:
    """anthropic-beta flags for `alias`: 1M context past the 200k default, plus server-side fallback
    when the model asks for it and `server_fallback` is set (direct Anthropic only; litellm passes False)."""
    entry = MODELS.get(alias)
    betas = []
    window = entry.get("context_window") if entry else None
    if window and window > 200_000:
        betas.append("context-1m-2025-08-07")
    if server_fallback and entry and entry.get("server_fallback"):
        betas.append("server-side-fallback-2026-07-01")
    return betas


def fallback_params(alias: str) -> dict:
    """Request params for server-side fallback on the direct Anthropic wire; must travel with anthropic_beta_headers()."""
    entry = MODELS.get(alias)
    return {"fallbacks": "default"} if entry and entry.get("server_fallback") else {}


# --- budgets ----------------------------------------------------------------
# Per model: B = context_window - max_output - PROMPT_RESERVE is the ceiling history is trimmed to.
# A fold triggers at TRIGGER_RATIO * B of backlog; the summary is capped at SUMMARY_CAP_FRACTION * B
# (and 90% of max_output). If summarizing fails the checkpoint must not advance, so nothing is dropped.
PROMPT_RESERVE = 20_000
TRIGGER_RATIO = 0.8
SUMMARY_CAP_FRACTION = 0.10

# Floor, and the fallback for aliases the catalog doesn't know.
DEFAULT_TRIM_BUDGET = 100_000

# Fallback when `alias` isn't registered: OpenRouter models are free text. Ollama derives its own
# budget from OLLAMA_NUM_CTX and never gets here.
BACKEND_DEFAULT_TRIM_BUDGET = {"openrouter": 1_000_000}


def trim_budget_for(alias: str, backend: str | None = None) -> int:
    entry = MODELS.get(alias)
    window = entry.get("context_window") if entry else None
    if not window:
        return BACKEND_DEFAULT_TRIM_BUDGET.get(backend, DEFAULT_TRIM_BUDGET)
    return max(DEFAULT_TRIM_BUDGET, window - entry.get("max_output", 0) - PROMPT_RESERVE)


def max_output_for(alias: str) -> int | None:
    entry = MODELS.get(alias)
    return entry.get("max_output") if entry else None


def compaction_trigger_for(alias: str, budget: int | None = None) -> int:
    """Backlog tokens since the last checkpoint that trigger a fold; `budget` overrides B (Ollama)."""
    return int(TRIGGER_RATIO * (budget or trim_budget_for(alias)))


def summary_cap_for(alias: str, budget: int | None = None) -> int:
    """Max checkpoint summary size; `budget` overrides B."""
    cap = int(SUMMARY_CAP_FRACTION * (budget or trim_budget_for(alias)))
    max_output = max_output_for(alias)
    return min(cap, int(max_output * 0.9)) if max_output else cap
