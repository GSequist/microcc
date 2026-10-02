import asyncio
from urllib.request import urlopen
import json
import os


def _is_source_checkout():
    """True when this module is running straight from a source tree (an
    editable install, or PYTHONPATH pointed at src/) rather than a normal
    site-packages copy. A real pip install always unpacks into a
    site-packages (or dist-packages) dir, so the absence of that segment
    means there's local source here to clobber — e.g. this repo's own dev
    venv, where --force-reinstall once overwrote an editable install with
    the published wheel mid-development."""
    this_file = os.path.abspath(__file__)
    return "site-packages" not in this_file and "dist-packages" not in this_file


def _version_tuple(v):
    parts = []
    for p in v.split("."):
        digits = "".join(ch for ch in p if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


async def check_and_update(app, current_version):
    """Fetch latest PyPI version without blocking the UI (screen paints
    first, this runs after). Never installs anything itself — the app
    stays fully usable — it only surfaces a pink notice in the static
    hint bar telling the user to run /update, which does the actual
    install (see tui_native/screen_cmds_.py's _run_update)."""

    if _is_source_checkout():
        return

    loop = asyncio.get_event_loop()

    def _fetch_latest():
        resp = urlopen("https://pypi.org/pypi/micro-cc/json", timeout=3)
        return json.loads(resp.read()).get("info", {}).get("version", "")

    try:
        latest = await loop.run_in_executor(None, _fetch_latest)
    except Exception:
        return  # offline or PyPI unreachable — silently retry next start

    # Strict > only — PyPI's JSON endpoint can lag behind a just-published
    # version for a few minutes, and a stale read here must never look
    # like a downgrade target.
    if not latest or _version_tuple(latest) <= _version_tuple(current_version):
        return

    app._update_available = latest
    app._static_hint_text()
