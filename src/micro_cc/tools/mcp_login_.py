"""/mcp-login and /mcp-logout core, shared by TUI and GUI: find OAuth MCP servers, log in, forget tokens."""

import asyncio
import time
from pathlib import Path
from typing import Callable, TypedDict

from micro_cc.tools import mcp_oauth
from micro_cc.tools.mcp_client_ import _has_static_auth, _is_stdio
from micro_cc.tools.search_tool_ import GLOBAL_MCPS_DIR, MCP_CATALOG, _scan_mcps_dir


class OAuthServer(TypedDict):
    name: str
    url: str                  # raw catalog url = token store key
    cfg: dict | None          # entry["oauth"] when it is a dict (client_id, scope, ...)
    status: str               # "logged in · expires in 42m", "not logged in", ...
    has_token: bool


_pending: dict[str, asyncio.Task] = {}   # url -> running login


def _catalog(project_dir: str) -> dict:
    """Fresh scan (not the cached one) so servers added to mcps/ show up without a restart."""
    merged = dict(MCP_CATALOG)
    merged.update(_scan_mcps_dir(GLOBAL_MCPS_DIR))
    if project_dir:
        merged.update(_scan_mcps_dir(Path(project_dir) / "mcps"))
    return merged


def _status(url: str, tok: dict) -> str:
    if url in _pending and not _pending[url].done():
        return "login in progress"
    if not tok.get("access_token"):
        return "not logged in"
    left = (tok.get("expires_at") or 0) - time.time()
    if not tok.get("expires_at"):
        return "logged in"
    if left > 0:
        return f"logged in · expires in {int(left // 3600)}h{int(left % 3600 // 60)}m" if left >= 3600 else f"logged in · expires in {int(left // 60)}m"
    return "expired · will refresh on next use" if tok.get("refresh_token") else "expired · login needed"


async def oauth_servers(project_dir: str) -> list[OAuthServer]:
    """Servers that use OAuth: `"oauth"` set, a stored token, or (no auth configured) a 401 on probe."""
    store = mcp_oauth.stored_tokens()
    sure, maybe = [], []
    for name, entry in _catalog(project_dir).items():
        server = entry["server"]
        if _is_stdio(server) or entry.get("oauth") is False:
            continue
        url = server["url"]
        if entry.get("oauth") or url in store:
            sure.append((name, entry))
        elif not _has_static_auth(entry):
            maybe.append((name, entry))
    probed = await asyncio.gather(*(mcp_oauth.requires_auth(e["server"]["url"]) for _, e in maybe))
    rows: list[OAuthServer] = []
    for name, entry in sure + [m for m, ok in zip(maybe, probed) if ok]:
        url = entry["server"]["url"]
        oauth = entry.get("oauth")
        tok = store.get(url, {})
        rows.append({"name": name, "url": url, "cfg": oauth if isinstance(oauth, dict) else None,
                     "status": _status(url, tok), "has_token": bool(tok.get("access_token"))})
    return sorted(rows, key=lambda r: r["name"])


async def _find(project_dir: str, name: str) -> tuple[OAuthServer | None, list[OAuthServer]]:
    rows = await oauth_servers(project_dir)
    return next((r for r in rows if r["name"].lower() == name.lower()), None), rows


def format_list(rows: list[OAuthServer]) -> str:
    if not rows:
        return "No OAuth MCP servers found (global ~/.micro-cc/mcps or the project's mcps/)."
    return "OAuth MCP servers:\n" + "\n".join(f"  {r['name']} — {r['status']}" for r in rows)


async def start_login(project_dir: str, name: str, notify: Callable[[str], None]) -> str:
    """Start a browser login in the background; login prompts and the outcome go to notify.
    Returns an immediate message (started / unknown server / already running)."""
    row, rows = await _find(project_dir, name)
    if row is None:
        names = ", ".join(r["name"] for r in rows) or "none"
        return f"'{name}' is not an OAuth MCP server (OAuth servers: {names})"
    url = row["url"]
    if url in _pending and not _pending[url].done():
        return f"login for {row['name']} already in progress — finish it in the browser"

    async def run():
        with mcp_oauth.auth_listener(notify):
            try:
                await mcp_oauth.force_login(url, row["cfg"])
                notify(f"MCP login for {row['name']} succeeded — its tools use the new token on the next call")
            except Exception as e:
                notify(f"MCP login for {row['name']} failed: {e}")
            finally:
                _pending.pop(url, None)

    _pending[url] = asyncio.get_running_loop().create_task(run())
    return f"starting MCP login for {row['name']}…"


async def logout(project_dir: str, name: str) -> str:
    row, rows = await _find(project_dir, name)
    if row is None or not row["has_token"]:
        names = ", ".join(r["name"] for r in rows if r["has_token"]) or "none"
        return f"no stored login for '{name}' (logged in: {names})"
    mcp_oauth.clear_oauth(row["url"])
    return f"forgot the OAuth login for {row['name']} — its next call will ask you to log in"
