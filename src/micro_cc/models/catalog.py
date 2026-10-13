"""Model catalog: providers, models and login prompts from catalog.json (schema 2).

Layers merge key by key, later wins, once at import and without network access:
  1. catalog.json shipped in this package - the offline fallback
  2. ~/.micro-cc/cache/catalog.json - GitHub's copy, refreshed in the background by catalog_sync.py
  3. ~/.micro-cc/models.json - legacy schema-1 user overrides, translated on read
  4. ~/.micro-cc/catalog.json - the user's own overrides, extra models and providers
A missing or broken layer is skipped; a bad entry is skipped and reported in CATALOG_STATUS["skipped"].
"""

import json
import os

PACKAGED_PATH = os.path.join(os.path.dirname(__file__), "catalog.json")
SCHEMA = 2
APIS = ("anthropic-messages", "openai-chat", "openai-responses")
# Schema-1 backend columns, translated by from_legacy.
LEGACY_BACKENDS = ("anthropic", "foundry", "litellm", "openai")


# --- layers -----------------------------------------------------------------

def remote_cache_path() -> str:
    return os.path.join(os.path.expanduser("~"), ".micro-cc", "cache", "catalog.json")


def user_catalog_path() -> str:
    return os.path.join(os.path.expanduser("~"), ".micro-cc", "catalog.json")


def legacy_user_path() -> str:
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
    """(doc, status); status is 'absent', 'ok' or 'unreadable: <reason>'."""
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


def from_legacy(doc: dict) -> dict:
    """Schema-1 user overrides -> schema-2 partial doc (models + default_model only)."""
    models = {}
    for alias, e in (doc.get("models") if isinstance(doc.get("models"), dict) else {}).items():
        if not isinstance(e, dict):
            models[alias] = e  # let validation report it
            continue
        out = {k: e[k] for k in ("context_window", "max_output", "server_fallback", "min_version") if k in e}
        ids = {b: e[b] for b in LEGACY_BACKENDS if b in e}
        if "litellm" in ids and e.get("responses_api") and isinstance(ids["litellm"], str):
            ids["litellm"] = {"id": ids["litellm"], "api": "openai-responses"}
        if ids:
            out["ids"] = ids
        if e.get("thinking") is False:
            out["reasoning"] = None
        else:
            r = {}
            if "effort_levels" in e:
                r["levels"] = e["effort_levels"]
            if "default_effort" in e:
                r["default"] = e["default_effort"]
            if "summarized_display" in e:
                r["summarized"] = e["summarized_display"]
            if r or e.get("thinking") is True:
                out["reasoning"] = r
        models[alias] = out
    out = {"models": models}
    if "default_model" in doc:
        out["default_model"] = doc["default_model"]
    return out


def _read_layers(status: dict) -> list:
    """[(layer name, schema-2 doc)] for the layers that loaded, packaged first. Packaged must load."""
    packaged, packaged_status = _read_layer(PACKAGED_PATH)
    if packaged is None:
        raise RuntimeError(f"micro-cc is missing its shipped catalog.json ({packaged_status})")
    layers = [("packaged", packaged)]
    for name, path in (("remote", remote_cache_path()), ("legacy", legacy_user_path()), ("user", user_catalog_path())):
        doc, status[name] = _read_layer(path)
        if doc is not None:
            layers.append((name, from_legacy(doc) if name == "legacy" else doc))
    return layers


# --- validation -------------------------------------------------------------

def _pos_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and v > 0


def _check_ids(v) -> dict:
    if not isinstance(v, dict):
        raise ValueError("ids must be an object")
    out = {}
    for provider, ref in v.items():
        if isinstance(ref, str) and ref:
            out[provider] = {"id": ref}
        elif isinstance(ref, dict) and isinstance(ref.get("id"), str) and ref["id"] and ref.get("api", APIS[0]) in APIS:
            out[provider] = {k: ref[k] for k in ("id", "api") if k in ref}
        else:
            raise ValueError(f"ids.{provider} must be a model id or {{id, api}}")
    return out


def _check_reasoning(v):
    if v is None:
        return None
    if not isinstance(v, dict):
        raise ValueError("reasoning must be an object or null")
    out = {}
    if "levels" in v:
        if not isinstance(v["levels"], list) or not all(isinstance(x, str) and x for x in v["levels"]):
            raise ValueError("reasoning.levels must be a list of strings")
        out["levels"] = v["levels"]
    if "default" in v:
        if v["default"] is not None and not isinstance(v["default"], str):
            raise ValueError("reasoning.default must be a string or null")
        out["default"] = v["default"]
    if "summarized" in v:
        if not isinstance(v["summarized"], bool):
            raise ValueError("reasoning.summarized must be true/false")
        out["summarized"] = v["summarized"]
    return out


def _check_model(e) -> dict:
    """Validated fields of one model entry (unknown keys dropped); raises ValueError."""
    if not isinstance(e, dict):
        raise ValueError("not an object")
    out = {}
    for k, v in e.items():
        if k == "name":
            if not isinstance(v, str):
                raise ValueError("name must be a string")
        elif k in ("context_window", "max_output"):
            if not _pos_int(v):
                raise ValueError(f"{k} must be a positive integer")
        elif k == "server_fallback":
            if not isinstance(v, bool):
                raise ValueError(f"{k} must be true/false")
        elif k == "ids":
            v = _check_ids(v)
        elif k == "reasoning":
            v = _check_reasoning(v)
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
            entries[alias] = _check_model(e)
        except ValueError as err:
            skipped.append(f"{alias}: {err}")
    default = doc.get("default_model")
    return entries, default if isinstance(default, str) else None, skipped


_PROVIDER_STR = ("name", "base_url", "base_url_env", "api_key_env", "api_key", "ids_from", "model_env",
                 "region", "region_env", "project_env")
CLIENTS = ("bedrock", "vertex")


def _check_provider(p) -> dict:
    """Validated fields of one provider (unknown or mistyped keys dropped)."""
    if not isinstance(p, dict):
        return {}
    out = {k: p[k] for k in _PROVIDER_STR if isinstance(p.get(k), str)}
    if p.get("api") in APIS:
        out["api"] = p["api"]
    if p.get("auth") == "oauth":
        out["auth"] = "oauth"
    if p.get("client") in CLIENTS:
        out["client"] = p["client"]
    pkg = p.get("package")
    if isinstance(pkg, dict) and isinstance(pkg.get("import"), str) and isinstance(pkg.get("pip"), str):
        out["package"] = {"import": pkg["import"], "pip": pkg["pip"]}
    if isinstance(p.get("free_text"), bool):
        out["free_text"] = p["free_text"]
    if isinstance(p.get("requires"), list) and all(isinstance(x, str) for x in p["requires"]):
        out["requires"] = p["requires"]
    if isinstance(p.get("login"), dict) and all(isinstance(v, str) for v in p["login"].values()):
        out["login"] = p["login"]
    t = p.get("first_event_timeout")
    if isinstance(t, (int, float)) and not isinstance(t, bool) and t > 0:
        out["first_event_timeout"] = t
    if isinstance(p.get("compat"), dict):
        out["compat"] = {k: v for k, v in p["compat"].items() if isinstance(v, (str, bool, int, float))}
    return out


# --- merging ----------------------------------------------------------------

_NESTED = ("ids", "reasoning", "compat", "login")


def _merge(cur: dict, fields: dict) -> None:
    """Override only the fields a layer sets; ids/reasoning/compat/login merge one level deep."""
    for k, v in fields.items():
        if k in _NESTED and isinstance(v, dict) and isinstance(cur.get(k), dict):
            cur[k] = {**cur[k], **v}
        else:
            cur[k] = v


def build_catalog() -> dict:
    """Every layer merged: models, options (file order), default, providers, routing, login_optional, status."""
    status = {"skipped": []}
    models, options, default = {}, [], None
    providers, routing, login_optional = {}, [], {}
    for name, doc in _read_layers(status):
        entries, layer_default, skipped = validate_catalog(doc)
        status["skipped"] += [f"{name} {s}" for s in skipped]
        for alias, fields in entries.items():
            if alias in models:
                _merge(models[alias], fields)
            elif fields.get("ids") and "context_window" in fields and "max_output" in fields:
                models[alias] = {"reasoning": None, **fields}
                options.append(alias)
            else:
                status["skipped"].append(f"{name} {alias}: new alias needs ids, context_window and max_output")
        if layer_default in models:
            default = layer_default

        for pid, p in (doc.get("providers") if isinstance(doc.get("providers"), dict) else {}).items():
            fields = _check_provider(p)
            if pid in providers:
                _merge(providers[pid], fields)
            elif "name" in fields and "api" in fields:
                providers[pid] = fields
            else:
                status["skipped"].append(f"{name} provider {pid}: new provider needs name and a known api")
        if isinstance(doc.get("routing"), list):
            routing = [x for x in doc["routing"] if isinstance(x, str)]
        if isinstance(doc.get("login_optional"), dict):
            login_optional.update({k: v for k, v in doc["login_optional"].items() if isinstance(v, str)})

    routing = [p for p in routing if p in providers] + [p for p in providers if p not in routing]
    return {"models": models, "options": options, "default": default, "providers": providers,
            "routing": routing, "login_optional": login_optional, "status": status}


# What shipped with this version; tests compare layers against it.
_PACKAGED_DOC = _read_layer(PACKAGED_PATH)[0] or {}
_BUILTIN_MODELS, _BUILTIN_DEFAULT, _ = validate_catalog(_PACKAGED_DOC)
_BUILTIN_OPTIONS = list(_BUILTIN_MODELS)

_CATALOG = build_catalog()
MODELS, MODEL_OPTIONS, DEFAULT_MODEL = _CATALOG["models"], _CATALOG["options"], _CATALOG["default"]
PROVIDERS, ROUTING, LOGIN_OPTIONAL = _CATALOG["providers"], _CATALOG["routing"], _CATALOG["login_optional"]
CATALOG_STATUS = _CATALOG["status"]


# --- providers --------------------------------------------------------------

def provider_name(provider: str) -> str:
    return PROVIDERS.get(provider, {}).get("name", provider)


def compat(provider: str) -> dict:
    return PROVIDERS.get(provider, {}).get("compat", {})


def login_sequence(provider: str) -> list:
    """Env vars /login asks for, in order: the provider's own, then the optional extras."""
    own = list(PROVIDERS.get(provider, {}).get("login", {}))
    return own + [k for k in LOGIN_OPTIONAL if k not in own] if own else []


def login_prompt(key: str) -> str:
    for p in PROVIDERS.values():
        if key in p.get("login", {}):
            return p["login"][key]
    return LOGIN_OPTIONAL.get(key, key)


def models_for(provider: str, current_model: str | None) -> list:
    """Aliases the provider serves; free-text providers (Ollama, OpenRouter) only offer the current model."""
    p = PROVIDERS.get(provider, {})
    if p.get("free_text"):
        return [current_model] if current_model else []
    key = p.get("ids_from", provider)
    return [alias for alias in MODEL_OPTIONS if key in MODELS[alias].get("ids", {})]


def resolve(alias: str, provider: str) -> tuple:
    """(model id, api) for `alias` on `provider`. Unknown aliases pass through unchanged,
    so a typo fails at the API instead of silently running another model."""
    p = PROVIDERS.get(provider, {})
    ref = MODELS.get(alias, {}).get("ids", {}).get(p.get("ids_from", provider), {})
    return ref.get("id", alias), ref.get("api", p.get("api"))


# --- model capabilities -----------------------------------------------------

def supports_thinking(alias: str) -> bool:
    entry = MODELS.get(alias)
    return entry["reasoning"] is not None if entry else True


def wants_summarized_display(alias: str) -> bool:
    return bool(((MODELS.get(alias) or {}).get("reasoning") or {}).get("summarized"))


def effort_levels_for(alias: str) -> list:
    return list(((MODELS.get(alias) or {}).get("reasoning") or {}).get("levels", []))


def default_effort_for(alias: str):
    return ((MODELS.get(alias) or {}).get("reasoning") or {}).get("default")


def resolve_effort(alias: str, requested):
    """`requested` if the model supports it, else the model's own default."""
    return requested if requested in effort_levels_for(alias) else default_effort_for(alias)


def max_output_for(alias: str) -> int | None:
    entry = MODELS.get(alias)
    return entry.get("max_output") if entry else None


def anthropic_beta_headers(alias: str, server_fallback: bool = True) -> list:
    """anthropic-beta flags: 1M context past the 200k default, plus server-side fallback
    when the model asks for it and `server_fallback` is set (direct Anthropic only)."""
    entry = MODELS.get(alias)
    betas = []
    window = entry.get("context_window") if entry else None
    if window and window > 200_000:
        betas.append("context-1m-2025-08-07")
    if server_fallback and entry and entry.get("server_fallback"):
        betas.append("server-side-fallback-2026-07-01")
    return betas


def fallback_params(alias: str) -> dict:
    """Request params for server-side fallback; must travel with anthropic_beta_headers()."""
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

# Fallback for unregistered aliases on free-text providers that serve big-context models.
PROVIDER_DEFAULT_TRIM_BUDGET = {"openrouter": 1_000_000}

# Ollama sizes its own window from OLLAMA_NUM_CTX; small windows need a smaller reserve and floor.
OLLAMA_PROMPT_RESERVE = 6000  # History only; system, tools, context uncounted
OLLAMA_MIN_TRIM_BUDGET = 2000


def ollama_trim_budget(num_ctx: int | None = None, max_output: int | None = None) -> int:
    num_ctx = num_ctx or int(os.getenv("OLLAMA_NUM_CTX") or 32768)
    max_output = max_output or int(os.getenv("OLLAMA_MAX_OUTPUT") or 4096)
    return max(OLLAMA_MIN_TRIM_BUDGET, num_ctx - max_output - OLLAMA_PROMPT_RESERVE)


def trim_budget_for(alias: str, provider: str | None = None) -> int:
    entry = MODELS.get(alias)
    window = entry.get("context_window") if entry else None
    if not window:
        return PROVIDER_DEFAULT_TRIM_BUDGET.get(provider, DEFAULT_TRIM_BUDGET)
    return max(DEFAULT_TRIM_BUDGET, window - entry.get("max_output", 0) - PROMPT_RESERVE)


def effective_trim_budget(alias: str, provider: str | None = None) -> int:
    """History budget for `alias` on `provider` (default: active): MICRO_CC_TRIM_BUDGET, Ollama's env window, else the catalog."""
    override = os.getenv("MICRO_CC_TRIM_BUDGET")
    if override:
        return int(override)
    if provider is None:
        from micro_cc.models.providers import active_provider
        provider = active_provider()
    if provider == "ollama":
        return ollama_trim_budget()
    return trim_budget_for(alias, provider)


def compaction_trigger_for(alias: str, budget: int | None = None) -> int:
    """Backlog tokens since the last checkpoint that trigger a fold; `budget` overrides B (Ollama)."""
    return int(TRIGGER_RATIO * (budget or trim_budget_for(alias)))


def summary_cap_for(alias: str, budget: int | None = None) -> int:
    """Max checkpoint summary size; `budget` overrides B."""
    cap = int(SUMMARY_CAP_FRACTION * (budget or trim_budget_for(alias)))
    max_output = max_output_for(alias)
    return min(cap, int(max_output * 0.9)) if max_output else cap
