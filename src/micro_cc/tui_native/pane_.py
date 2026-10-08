"""Mod side pane: open/close, focus, and routing of keys and mouse into it."""

from micro_cc import mods_
from micro_cc.utils import settings_store_


SIDES = ("right", "top", "bottom")


def open_pane(app, name: str, width="40%", persist: bool = True, side: str = "right") -> bool:
    """Show mod pane `name` right of, above or below the conversation; replaces any open pane. width is rows for top/bottom."""
    if side not in SIDES:
        if persist:
            app._flash_status(f"pane side must be one of {', '.join(SIDES)}", seconds=3)
        return False
    if not mods_.has("pane", name=name):
        if persist:
            app._flash_status(f"no mod draws a pane named {name!r}", seconds=3)
        return False
    cache = {}

    def draw(w, h):
        key = (mods_.generation(), w, h, app._pane_focused)
        if cache.get("key") != key:
            cache["key"], cache["lines"] = key, mods_.render_pane(name, w, h, app._pane_focused)
        return cache["lines"]

    app._pane_name = name
    app.main_split.pane = draw
    app.main_split.pane_width = width
    app.main_split.side = side
    if persist:
        settings_store_.edit_setting("pane", {"name": name, "width": width, "side": side})
    app.request_render()
    return True


def close_pane(app) -> None:
    """Explicit close: also forgets it, so the next start doesn't reopen it."""
    app._pane_name = None
    app._pane_focused = False
    app.main_split.pane = None
    settings_store_.edit_setting("pane", {})
    app.request_render()


def restore(app) -> None:
    """Reopen the pane open at last exit if a loaded mod still draws it; never raises into boot."""
    try:
        pref = settings_store_.get_setting("pane") or {}
        if isinstance(pref, dict) and pref.get("name"):
            open_pane(app, str(pref["name"]), pref.get("width", "40%"), persist=False, side=pref.get("side", "right"))
    except Exception as e:
        mods_.record_error("pane restore", f"{type(e).__name__}: {e}")


def on_closed(app) -> None:
    """HSplit saw the pane return None (its mod got disabled); the saved pane is kept for next start."""
    app._pane_name = None
    app._pane_focused = False
    app.request_render()


def shown(app) -> bool:
    return app._pane_name is not None and app.main_split.rect is not None


def focus(app, on: bool = True) -> None:
    app._pane_focused = on and shown(app)
    mods_.bump()
    app.request_render()


def route_key(app, data: str, key_id: str) -> bool:
    """Toggle/leave pane focus, or feed keys to a focused pane. True if handled here."""
    if app.get_focus() is not app.prompt:
        app._pane_focused = False  # a picker or question took over; it gets the keys
        return False
    if app._pane_focused and not shown(app):
        app._pane_focused = False  # collapsed by a resize or closed under us
    if key_id == "ctrl+]" and app._pane_name is not None:
        focus(app, not app._pane_focused)
        return True
    if not app._pane_focused:
        return False
    if key_id == "escape":
        focus(app, False)
        return app._input_mode == "idle"  # mid-turn, the same Esc also interrupts
    if key_id == "ctrl+c":
        return False
    mods_.pane_key(app._pane_name, data)
    app.request_render()
    return True  # a focused pane swallows unconsumed keys instead of typing into the prompt


def route_mouse(app, mouse: dict) -> bool:
    """Send a mouse event inside the pane to its mod; a drag-select already in progress keeps it."""
    rect, top = app.main_split.rect, app.root.find_offset(app.main_split)
    if app._pane_name is None or rect is None or top is None or app.tui.selection_press_active:
        return False
    if app.tui.overlay_hit(mouse["x"], mouse["y"]) is not None:
        return False
    col, row, w, h = rect
    x, y = mouse["x"] - col, mouse["y"] - top - row
    if not (0 <= x < w and 0 <= y < h):
        return False
    if mouse["button"] == 0 and not mouse["release"]:
        app._pane_focused = True  # clicking into the pane focuses it
    mods_.pane_click(app._pane_name, x, y, mouse["button"], mouse["release"])
    app.request_render()
    return True
