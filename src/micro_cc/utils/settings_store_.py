import datetime
import json
import os
from pathlib import Path
from micro_cc.models import registry as model_registry
from micro_cc.postgres_store import pg_store_

_SETTINGS_PATH = Path.home() / ".micro-cc" / "settings.json"

# Mirrors claude_loop_.DEFAULT_DANGEROUS (duplicated to avoid circular import).
_DEFAULT_DANGEROUS = ["bash_", "edit_", "write_"]
DEFAULT_MAX_SUBAGENTS = 4


def _use_postgres() -> bool:
    """Routes to Postgres if MICRO_CC_POSTGRES_URL is set (global only)."""
    return bool(os.getenv("MICRO_CC_POSTGRES_URL"))


def _defaults() -> dict:
    return {
        "dangerous": _DEFAULT_DANGEROUS,
        "model": model_registry.DEFAULT_MODEL,
        "tokens_budget": model_registry.DEFAULT_TRIM_BUDGET,
        # Max concurrent subagents (enforced at spawn; /subagents changes it).
        "max_subagents": DEFAULT_MAX_SUBAGENTS,
        # Mod side pane open at exit, {"name", "width"}; {} = none. Reopened at TUI start.
        "pane": {},
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


def max_subagents() -> int:
    """Concurrent subagent cap, with fallback to default on invalid value."""
    try:
        n = int(get_setting("max_subagents"))
    except (TypeError, ValueError):
        return DEFAULT_MAX_SUBAGENTS
    return n if n >= 1 else DEFAULT_MAX_SUBAGENTS
