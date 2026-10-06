import asyncio
from urllib.request import urlopen
import json
import os


def _is_source_checkout():
    """True when running from a source tree (editable install or PYTHONPATH), not site-packages."""
    this_file = os.path.abspath(__file__)
    return "site-packages" not in this_file and "dist-packages" not in this_file


def _version_tuple(v):
    parts = []
    for p in v.split("."):
        digits = "".join(ch for ch in p if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


async def check_and_update(app, current_version):
    """Check PyPI for a newer version off the UI path; shows a /update notice, never installs."""

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

    # Strict >: PyPI JSON can lag a fresh publish; a stale read must not look like a downgrade.
    if not latest or _version_tuple(latest) <= _version_tuple(current_version):
        return

    app._update_available = latest
    app._static_hint_text()
