"""Color set store from ~/.micro-cc/theme.json, read at render time, not cached at import."""

import json
import os
from pathlib import Path

from micro_cc.postgres_store import pg_store_

_THEME_PATH = Path.home() / ".micro-cc" / "theme.json"

# Required tokens — typos raise KeyError instead of painting invisible.
TOKENS = (
    "bg", "fg", "accent", "warn", "error", "ok", "info", "paused", "running",
    "queued", "code_bg", "code_fg", "inline_code", "link", "link_url",
    "heading", "quote", "table_border", "table_header", "list",
    "diff_add_bg", "diff_del_bg", "diff_add_fg", "diff_del_fg", "syntax",
    "banner_colors", "banner_dim",
)

# Seed presets; "white" is the default for fresh installs.
PRESETS = {
    "pitch": {
        "bg": "#000000",
        "fg": "#d0d0d0",
        "accent": "#e07fb0",
        "warn": "#e5c07b",
        "error": "#e06c75",
        "ok": "#98c379",
        "info": "#6cbcc8",
        "paused": "#c38fd6",
        "running": "#6cbcc8",
        "queued": "#d19a66",
        "code_bg": "#000000",
        "code_fg": "#8cc8d4",
        "inline_code": "#8cc8d4",
        "link": "#7aa8e0",
        "link_url": "#5f7f9f",
        "heading": "#c9a0dc",
        "quote": "#9a8aa6",
        "table_border": "#3e4a50",
        "table_header": "#8cc8d4",
        "list": "#8cc8d4",
        "diff_add_bg": "#10301a",
        "diff_del_bg": "#3a1414",
        "diff_add_fg": "#98c379",
        "diff_del_fg": "#e06c75",
        "syntax": "monokai",
        "banner_colors": ["#4a8a68", "#8fa8d9", "#c99bd9", "#d98aa3",
                          "#d9a789", "#7ac9c0", "#c9c17a"],
        "banner_dim": "#6c6c6c",
    },
    "white": {
        "bg": "#ffffff",
        "fg": "#24292f",
        "accent": "#c2185b",
        "warn": "#a86a00",
        "error": "#c62828",
        "ok": "#2e7d32",
        "info": "#1565c0",
        "paused": "#8e24aa",
        "running": "#1565c0",
        "queued": "#b36200",
        "code_bg": "#f3f3f3",
        "code_fg": "#24292f",
        "inline_code": "#b02a6b",
        "link": "#0b57d0",
        "link_url": "#0b57d0",
        "heading": "#0b3d91",
        "quote": "#555555",
        "table_border": "#888888",
        "table_header": "#24292f",
        "list": "#0b3d91",
        "diff_add_bg": "#ddf5e0",
        "diff_del_bg": "#ffe3e3",
        "diff_add_fg": "#2e7d32",
        "diff_del_fg": "#c62828",
        "syntax": "friendly",
        "banner_colors": ["#3d7d5e", "#5b7fc4", "#a86fc4", "#c45f85",
                          "#c47f52", "#3fa89e", "#a89a3f"],
        "banner_dim": "#9a9a9a",
    },
    # Dieter Rams palette: off-white body, warm greys, one accent.
    "rams": {
        "bg": "#F2F0EB",
        "fg": "#2B2A26",
        "accent": "#B5715A",
        "warn": "#C2A05E",
        "error": "#B3706E",
        "ok": "#6F9478",
        "info": "#7793B4",
        "paused": "#9C86AE",
        "running": "#7793B4",
        "queued": "#C2A05E",
        "code_bg": "#E9E6DF",
        "code_fg": "#2B2A26",
        "inline_code": "#A4665A",
        "link": "#5F7FA6",
        "link_url": "#8C8981",
        "heading": "#3A3833",
        "quote": "#8C8981",
        "table_border": "#D8D4C9",
        "table_header": "#3A3833",
        "list": "#3A3833",
        "diff_add_bg": "#E3EDE3",
        "diff_del_bg": "#F2E4E2",
        "diff_add_fg": "#5F8A66",
        "diff_del_fg": "#A86863",
        "syntax": "friendly",
        "banner_colors": ["#6F9478", "#7793B4", "#9C86AE", "#B5715A",
                          "#C2A05E", "#6F9C94", "#A79B62"],
        "banner_dim": "#A9A69D",
    },
    # Rams palette on warm near-black.
    "rams dark": {
        "bg": "#1E1E1C",
        "fg": "#DAD7D0",
        "accent": "#D8A88C",
        "warn": "#DCC08A",
        "error": "#D49A96",
        "ok": "#9BC0A4",
        "info": "#9DB8D4",
        "paused": "#C3AAD0",
        "running": "#9DB8D4",
        "queued": "#DCC08A",
        "code_bg": "#262623",
        "code_fg": "#DAD7D0",
        "inline_code": "#E0B49C",
        "link": "#A8C0DC",
        "link_url": "#9A978F",
        "heading": "#EDEAE3",
        "quote": "#9A978F",
        "table_border": "#3A3A36",
        "table_header": "#EDEAE3",
        "list": "#EDEAE3",
        "diff_add_bg": "#253026",
        "diff_del_bg": "#332524",
        "diff_add_fg": "#9BC0A4",
        "diff_del_fg": "#D49A96",
        "syntax": "monokai",
        "banner_colors": ["#9BC0A4", "#9DB8D4", "#C3AAD0", "#D8A88C",
                          "#DCC08A", "#8FC0B6", "#C4B57E"],
        "banner_dim": "#6F6C66",
    },
}

DEFAULT_THEME = "pitch"

# Human labels for the /theme picker (the raw name is the picker's value).
THEME_LABELS = {
    "pitch": "pitch — pitch-black ground, soft text",
    "white": "white — white ground, near-black text",
    "rams": "rams — off-white ground, warm greys, one accent",
    "rams dark": "rams dark — the same palette on a warm near-black",
}


def _use_postgres() -> bool:
    """Same toggle as settings_store_/msg_store_._use_postgres."""
    return bool(os.getenv("MICRO_CC_POSTGRES_URL"))


def _seed(name: str = DEFAULT_THEME) -> dict:
    return {"name": name, "colors": dict(PRESETS[name])}


# Sticky flag for corrupt theme.json reset detection.
_was_reset = False


def was_reset_for_corruption() -> bool:
    return _was_reset


def _backfill(store: dict) -> dict:
    """Fill missing tokens with preset defaults to prevent KeyError on stale files."""
    preset = PRESETS.get(store.get("name"), PRESETS[DEFAULT_THEME])
    colors = store.setdefault("colors", {})
    missing = [t for t in TOKENS if t not in colors]
    if missing:
        for t in missing:
            colors[t] = preset[t]
        _save(store)
    return store


def _load() -> dict:
    global _was_reset
    if _use_postgres():
        store = pg_store_.load_theme()
        if store is None:
            _save(_seed())
            return _seed()
        return _backfill(store)

    if not _THEME_PATH.exists():
        _save(_seed())  # write-through, same reasoning as settings_store_
        return _seed()
    try:
        store = json.loads(_THEME_PATH.read_text())
    except json.JSONDecodeError:
        _was_reset = True
        _save(_seed())
        return _seed()
    return _backfill(store)


def _save(store: dict) -> None:
    if _use_postgres():
        pg_store_.save_theme(store)
        return
    _THEME_PATH.parent.mkdir(parents=True, exist_ok=True)
    _THEME_PATH.write_text(json.dumps(store, indent=2))


# Cached lazily on first colors()/get() call, not at import time.
_store: dict | None = None


def _current() -> dict:
    global _store
    if _store is None:
        _store = _load()
    return _store


def current_name() -> str:
    return _current().get("name", DEFAULT_THEME)


def colors() -> dict:
    """The active color set. Consumers call this at render time."""
    return _current().get("colors", PRESETS[DEFAULT_THEME])


def get(token: str) -> str:
    """One color from the active set. Raises KeyError on an unknown token
    rather than returning None — a typo should fail loudly."""
    if token not in TOKENS:
        raise KeyError(f"unknown theme token {token!r}")
    return colors()[token]


# --- live switching ---
# alt_screen_ and start_live_tui_ register here so a switch repaints without a restart.
_listeners: list = []


def on_change(callback) -> None:
    _listeners.append(callback)


def _apply(store: dict) -> None:
    global _store
    _store = store
    _save(store)
    for cb in list(_listeners):
        try:
            cb(store.get("name", "custom"))
        except Exception:
            # Swallow errors; switch is already applied and saved.
            pass


def set_preset(name: str) -> str:
    """Swap the whole color set for a built-in preset. Returns a short
    status string meant to go straight back to the model/user."""
    if name not in PRESETS:
        return f"unknown theme {name!r} — options: {', '.join(PRESETS)}"
    _apply(_seed(name))
    return f"theme set to {name}"


def set_colors(color_set: dict, name: str = "custom") -> str:
    """Plug in an arbitrary color set — what /theme uses for a hand-edited
    set, and what a caller would use to apply colors read from anywhere."""
    missing = [t for t in TOKENS if t not in color_set]
    if missing:
        return f"color set is missing tokens: {', '.join(missing)}"
    _apply({"name": name, "colors": {t: color_set[t] for t in TOKENS}})
    return f"theme set to {name}"
