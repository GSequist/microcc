"""Background refresh of the remote model catalog cache (never called at import)."""

import json
import os
import threading
import time
import urllib.error
import urllib.request

from micro_cc.models.registry import remote_cache_path, validate_catalog

CATALOG_URL = "https://raw.githubusercontent.com/GSequist/microcc/main/models.json"
MIN_REFRESH_SECONDS = 6 * 3600
TIMEOUT_SECONDS = 2


def _cached_etag(path: str):
    try:
        with open(path, encoding="utf-8") as f:
            etag = json.load(f).get("_etag")
        return etag if isinstance(etag, str) else None
    except Exception:
        return None


def _write_atomic(path: str, doc: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(doc, f, indent=2)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def refresh_remote_catalog(url: str = CATALOG_URL, force: bool = False) -> str:
    """Fetch remote models.json into the cache; returns a short outcome, never raises."""
    try:
        path = remote_cache_path()
        if not force:
            try:
                if time.time() - os.path.getmtime(path) < MIN_REFRESH_SECONDS:
                    return "fresh"
            except OSError:
                pass
        req = urllib.request.Request(url)
        etag = _cached_etag(path)
        if etag:
            req.add_header("If-None-Match", etag)
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
                body, new_etag = resp.read(1_000_000), resp.headers.get("ETag")
        except urllib.error.HTTPError as e:
            if e.code == 304:
                os.utime(path)
                return "not-modified"
            return f"http-{e.code}"
        doc = json.loads(body)
        if not isinstance(doc, dict) or doc.get("schema") != 1 or validate_catalog(doc)[2] == ["malformed document"]:
            return "invalid"
        if new_etag:
            doc["_etag"] = new_etag
        _write_atomic(path, doc)
        return "updated"
    except Exception as e:
        return f"error: {type(e).__name__}"
