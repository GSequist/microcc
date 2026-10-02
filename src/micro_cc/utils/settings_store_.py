import datetime
import json
import os
from pathlib import Path
from micro_cc.models import registry as model_registry
from micro_cc.postgres_store import pg_store_

_SETTINGS_PATH = Path.home() / ".micro-cc" / "settings.json"

# Mirrors claude_loop_.DEFAULT_DANGEROUS. Duplicated (not imported) so this
# leaf utils module doesn't reach up into the orchestrator — claude_loop_
# will need to read settings_store_ for /setup, and importing DEFAULT_DANGEROUS
# from there would make that a circular import.
_DEFAULT_DANGEROUS = ["bash_", "edit_", "write_"]
DEFAULT_MAX_SUBAGENTS = 4


def _use_postgres() -> bool:
    """Same toggle as msg_store_/memory_store_._use_postgres — set
    MICRO_CC_POSTGRES_URL and every call in this module routes to Postgres
    instead of local disk. Global only (see pg_store_._ensure_settings_table)
    — settings.json has never been scoped per project_dir, so there's no
    project_hash split here the way memory_store_ has."""
    return bool(os.getenv("MICRO_CC_POSTGRES_URL"))


def _defaults() -> dict:
    return {
        "dangerous": _DEFAULT_DANGEROUS,
        "model": model_registry.DEFAULT_MODEL,
        "tokens_budget": model_registry.DEFAULT_TRIM_BUDGET,
        # Live headless subagents one boss may run at once (enforced at spawn
        # by subagent_tracker_.add_tracked). 4 suits a weak laptop; /subagents
        # changes it.
        "max_subagents": DEFAULT_MAX_SUBAGENTS,
    }


# Sticky for this process once a corrupt settings.json is reset to defaults —
# _load() heals the file immediately (so every later read is valid JSON
# again), so this can't be recomputed from disk state. Checked once at
# startup to swap the hint bar's rotating tip for a warning.
_was_reset = False


def was_reset_for_corruption() -> bool:
    return _was_reset


def _backfill(store: dict) -> dict:
    """An install from before a new setting was added (e.g. "deep_memory")
    has a stored dict missing that key. Merge in only what's missing — never
    overwrite a value the user already has — and write through so
    get_setting/edit_setting both see it from here on. Shared by both
    backends so a new default lands the same way regardless of which one is
    active."""
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
            _save(defaults)  # write-through, same reasoning as the local
            # missing-file case below — first read on a fresh
            # MICRO_CC_POSTGRES_URL should leave a real row behind, not just
            # an in-memory default.
            return defaults
        return _backfill(store)

    if not _SETTINGS_PATH.exists():
        defaults = _defaults()
        _save(defaults)  # write-through so ~/.micro-cc/settings.json is a
        # real file from the first read, not just an in-memory default —
        # anything reading it straight off disk (statusline.sh, /setup via
        # bash_) needs it to actually be there.
        return defaults
    try:
        store = json.loads(_SETTINGS_PATH.read_text())
    except json.JSONDecodeError:
        # Same treatment as a missing file: reset to defaults and write
        # through, so the app always boots clean instead of crashing or
        # limping along on a broken file.
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
    # tmp + rename: several processes read this file (every microcc-headless
    # subagent at startup). A plain write_text can be observed half-written,
    # which _load treats as corrupt and resets EVERY setting to defaults.
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
    """The concurrent-subagent cap, falling back to the default on a missing
    or garbled value rather than disabling the cap."""
    try:
        n = int(get_setting("max_subagents"))
    except (TypeError, ValueError):
        return DEFAULT_MAX_SUBAGENTS
    return n if n >= 1 else DEFAULT_MAX_SUBAGENTS
