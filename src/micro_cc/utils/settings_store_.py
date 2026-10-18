import json
import os
from pathlib import Path
from micro_cc.models import catalog as model_catalog
from micro_cc.postgres_store import pg_store_

_SETTINGS_PATH = Path.home() / ".micro-cc" / "settings.json"

_DEFAULT_DANGEROUS = ["bash_", "edit_", "write_"]
DEFAULT_MAX_SUBAGENTS = 4


def _use_postgres() -> bool:
    """Routes to Postgres if MICRO_CC_POSTGRES_URL is set (global only)."""
    return bool(os.getenv("MICRO_CC_POSTGRES_URL"))


def _defaults() -> dict:
    return {
        "dangerous": _DEFAULT_DANGEROUS,
        # True: a model check auto-allows gated calls it judges safe; asks only when unsure.
        "auto_gate": False,
        "model": model_catalog.DEFAULT_MODEL,
        "tokens_budget": model_catalog.DEFAULT_TRIM_BUDGET,
        # Reasoning effort picked in /model; None = the model's own default_effort.
        "reasoning_effort": None,
        # Max concurrent subagents (enforced at spawn; /subagents changes it).
        "max_subagents": DEFAULT_MAX_SUBAGENTS,
        # Mod side pane open at exit, {"name", "width"}; {} = none. Reopened at TUI start.
        "pane": {},
        # Last version whose release note was dismissed (utils/whats_new.py).
        "seen_version": None,
    }


# Tracks if corrupt settings were reset on this process startup.
_was_reset = False


def was_reset_for_corruption() -> bool:
    return _was_reset


def _backfill(store: dict) -> dict:
    """Merge missing defaults into old stores, write-through."""
    missing = {k: v for k, v in _defaults().items() if k not in store}
    if missing:
        store.update(missing)
        _save(store)
    return store


def _load() -> dict:
    global _was_reset
    if _use_postgres():
        store = pg_store_.load_settings()
        if store is None:
            defaults = _defaults()
            _save(defaults)  # write-through: persist on first read
            return defaults
        return _backfill(store)

    if not _SETTINGS_PATH.exists():
        defaults = _defaults()
        _save(defaults)  # write-through: ensure real file for direct reads
        return defaults
    try:
        store = json.loads(_SETTINGS_PATH.read_text())
    except json.JSONDecodeError:
        # Corrupt file: reset to defaults and write-through.
        _was_reset = True
        defaults = _defaults()
        _save(defaults)
        return defaults

    return _backfill(store)


def _save(store: dict) -> None:
    if _use_postgres():
        pg_store_.save_settings(store)
        return
    _SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    # Atomic write: tmp + rename avoids half-written reads from other processes.
    tmp = _SETTINGS_PATH.with_name(f"{_SETTINGS_PATH.name}.tmp.{os.getpid()}")
    tmp.write_text(json.dumps(store, indent=2))
    tmp.replace(_SETTINGS_PATH)


def read_settings():
    store = _load()
    items = [
        {
            "setting_key": k,
            "setting_value": v,
        }
        for k, v in store.items()
    ]
    return items


def get_setting(key: str):
    store = _load()
    if key not in store:
        return None
    return store[key]


def edit_setting(
    key: str, value: str):
    store = _load()
    if key not in store:
        return f"Missing setting key {key}"
    if value is not None:
        store[key] = value
    _save(store)
    return f"Setting for {key} saved"


def gated_tools() -> set:
    """Tools that ask before running; the one source of truth for TUI, web and claude_loop."""
    return set(get_setting("dangerous") or [])


def set_gated(tools) -> None:
    edit_setting("dangerous", sorted(set(tools)))


_ACTING_TOOLS = ["bash_", "write_", "edit_", "computer", "browser"]
# /dangerous presets for non-technical users; each sets the gated tools and auto_gate together.
GATE_PRESETS = {
    "careful": {"label": "Careful", "tools": _ACTING_TOOLS, "auto": False,
                "description": "always ask before running code, changing files or using the browser/computer"},
    "auto": {"label": "Auto", "tools": _ACTING_TOOLS, "auto": True,
             "description": "Claude checks each action and asks only when it's risky or unsure"},
    "trusted": {"label": "Trusted", "tools": [], "auto": False, "description": "never ask"},
}


def apply_preset(key: str) -> None:
    p = GATE_PRESETS[key]
    set_gated(p["tools"])
    edit_setting("auto_gate", p["auto"])


def current_preset() -> str:
    """Preset key matching the current settings, else "custom"."""
    gated, auto = gated_tools(), bool(get_setting("auto_gate"))
    return next((k for k, p in GATE_PRESETS.items() if set(p["tools"]) == gated and p["auto"] == auto), "custom")


def max_subagents() -> int:
    """Concurrent subagent cap, with fallback to default on invalid value."""
    try:
        n = int(get_setting("max_subagents"))
    except (TypeError, ValueError):
        return DEFAULT_MAX_SUBAGENTS
    return n if n >= 1 else DEFAULT_MAX_SUBAGENTS
