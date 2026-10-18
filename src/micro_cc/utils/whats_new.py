"""One-time release note per version, shown until the first message (TUI and GUI)."""

from importlib.metadata import version

from micro_cc.utils import settings_store_

# Add an entry per release; versions without one show nothing.
NOTES = {
    "0.2.110": [
        "Auto gate: a model check can allow gated calls it judges safe (turn on in /dangerous)",
        "Approval prompts now explain each call in plain language and show a risk badge",
        "Deleting files, sudo, uploads, system settings and git push always ask, even in Auto",
    ],
}


def pending() -> dict | None:
    """Note for the installed version if the user hasn't seen it yet."""
    v = version("micro-cc")
    lines = NOTES.get(v)
    if not lines or settings_store_.get_setting("seen_version") == v:
        return None
    return {"version": v, "lines": lines}


def mark_seen() -> None:
    settings_store_.edit_setting("seen_version", version("micro-cc"))
