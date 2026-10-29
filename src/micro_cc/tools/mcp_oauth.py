"""OAuth for MCP servers: RFC 9728/8414 discovery, client via pre-registration, CIMD (client ID metadata
document) or RFC 7591 dynamic registration, PKCE, loopback callback, refresh. Tokens live in
~/.micro-cc/mcp_oauth.json (0600), keyed by server URL.
Catalog opt-in: `"oauth": true` or `"oauth": {"client_id", "client_secret", "scope", "callback_port",
"client_metadata_url"}`; an https `client_id` is used as a CIMD client id as-is.
"""

import asyncio
import base64
import hashlib
import json
import os
import platform
import re
import secrets
import shutil
import subprocess
import sys
import time
import webbrowser
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

STORE = Path.home() / ".micro-cc" / "mcp_oauth.json"
LOGIN_TIMEOUT = 300
REFRESH_SKEW = 60
CLIENT_NAME = "micro-cc"
# Hosted CIMD doc (its URL is the client_id); empty = skip CIMD, use dynamic registration.
CLIENT_METADATA_URL = os.environ.get("MICROCC_OAUTH_CLIENT_METADATA_URL", "")

_locks: dict[str, asyncio.Lock] = {}
_listeners: set = set()


def _load() -> dict:
    try:
        return json.loads(STORE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write(data: dict) -> None:
    STORE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STORE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(STORE)


def _save(key: str, **fields) -> dict:
    data = _load()
    data[key] = {**data.get(key, {}), **fields}
    _write(data)
    return data[key]


def clear_oauth(server_url: str) -> None:
    """Forget stored client + tokens for a server."""
    data = _load()
    if data.pop(server_url, None) is not None:
        _write(data)


@contextmanager
def auth_listener(fn):
    """Route login prompts (auth URL notices) to fn while active; without listeners they go to stderr."""
    _listeners.add(fn)
    try:
        yield
    finally:
        _listeners.discard(fn)


def _announce(msg: str) -> None:
    for fn in list(_listeners):
        try:
            fn(msg)
        except Exception:
            pass
    if not _listeners:
        print(msg, file=sys.stderr, flush=True)


def _is_wsl() -> bool:
    return bool(os.environ.get("WSL_DISTRO_NAME")) or "microsoft" in platform.uname().release.lower()


def _open_browser(url: str) -> bool:
    """Best-effort open; False when there's no usable GUI browser (WSL without interop, SSH, headless)."""
    if _is_wsl():
        for cmd in (["wslview", url], ["rundll32.exe", "url.dll,FileProtocolHandler", url],
                    ["powershell.exe", "-NoProfile", "-Command", f"Start-Process '{url}'"]):
            if shutil.which(cmd[0]):
                try:
                    subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                     stdin=subprocess.DEVNULL, start_new_session=True)
                    return True
                except OSError:
                    continue
        return False
    # No display on Linux: webbrowser would fall back to a text browser and grab the terminal.
    if sys.platform.startswith("linux") and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        return False
    try:
        return webbrowser.open(url)
    except Exception:
        return False


def _copy(text: str) -> bool:
    """Best-effort clipboard copy (wrapped URLs are painful to select in a TUI)."""
    for cmd in (["clip.exe"], ["pbcopy"], ["wl-copy"], ["xclip", "-selection", "clipboard"]):
        if shutil.which(cmd[0]):
            try:
                subprocess.run(cmd, input=text.encode("utf-16le" if cmd[0] == "clip.exe" else "utf-8"),
                               timeout=3, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return True
            except Exception:
                continue
    return False


async def _get_json(http: httpx.AsyncClient, url: str) -> dict | None:
    try:
        r = await http.get(url, headers={"Accept": "application/json"})
        return r.json() if r.status_code == 200 else None
    except Exception:
        return None


async def _challenge(http: httpx.AsyncClient, server_url: str) -> str:
    """WWW-Authenticate header from an unauthenticated probe (GET, then POST initialize)."""
    probes = (("GET", {"Accept": "text/event-stream"}, None),
              ("POST", {"Accept": "application/json, text/event-stream"},
               {"jsonrpc": "2.0", "id": 0, "method": "initialize",
                "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                           "clientInfo": {"name": CLIENT_NAME, "version": "0"}}}))
    for method, headers, body in probes:
        try:
            r = await http.request(method, server_url, headers=headers, json=body)
            if h := r.headers.get("www-authenticate"):
                return h
        except Exception:
            pass
    return ""


async def _discover(http: httpx.AsyncClient, server_url: str) -> dict:
    """Return AS metadata (authorization/token/registration endpoints) plus `_scope`/`_resource` hints."""
    u = urlparse(server_url)
    origin = f"{u.scheme}://{u.netloc}"
    path = u.path.rstrip("/")

    challenge = await _challenge(http, server_url)
    m = re.search(r'resource_metadata="?([^",\s]+)', challenge)
    s = re.search(r'scope="([^"]*)"', challenge)
    prm = None   # protected-resource metadata
    for url in (m and m.group(1), f"{origin}/.well-known/oauth-protected-resource{path}",
                f"{origin}/.well-known/oauth-protected-resource"):
        if url and (prm := await _get_json(http, url)):
            break

    as_url = ((prm or {}).get("authorization_servers") or [origin])[0].rstrip("/")
    a = urlparse(as_url)
    a_origin, a_path = f"{a.scheme}://{a.netloc}", a.path
    candidates = [f"{a_origin}/.well-known/oauth-authorization-server{a_path}",
                  f"{a_origin}/.well-known/openid-configuration{a_path}",
                  f"{as_url}/.well-known/openid-configuration"]
    for url in candidates:
        if meta := await _get_json(http, url):
            break
    else:   # legacy servers without metadata: default endpoint paths
        meta = {"authorization_endpoint": f"{a_origin}/authorize", "token_endpoint": f"{a_origin}/token",
                "registration_endpoint": f"{a_origin}/register"}
    meta["_scope"] = (s.group(1) if s else "") or " ".join(
        (prm or {}).get("scopes_supported") or meta.get("scopes_supported") or [])
    meta["_resource"] = (prm or {}).get("resource") or server_url
    return meta


async def _register(http: httpx.AsyncClient, meta: dict, redirect_uri: str, scope: str) -> dict:
    """Dynamic client registration as a public client (PKCE, no secret)."""
    endpoint = meta.get("registration_endpoint")
    if not endpoint:
        raise RuntimeError("server supports neither dynamic client registration nor a usable client ID "
                           "metadata document; set oauth.client_id (and client_secret) in the mcp config")
    body = {"client_name": CLIENT_NAME, "redirect_uris": [redirect_uri],
            "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"],
            "token_endpoint_auth_method": "none"}
    if scope:
        body["scope"] = scope
    r = await http.post(endpoint, json=body)
    if r.status_code >= 400:
        raise RuntimeError(f"client registration failed ({r.status_code}): {r.text[:300]}")
    info = r.json()
    return {"client_id": info["client_id"], "client_secret": info.get("client_secret")}


def _pick_client(meta: dict, cfg: dict, stored: dict, redirect_uri: str) -> dict | None:
    """Pre-registered > stored (same redirect, or CIMD) > CIMD if the AS supports it; None = register."""
    if cfg.get("client_id"):
        return {"client_id": cfg["client_id"], "client_secret": cfg.get("client_secret")}
    sid = stored.get("client_id", "")
    if sid and (stored.get("redirect_uri") == redirect_uri or sid.startswith("https://")):
        return {"client_id": sid, "client_secret": stored.get("client_secret")}
    cimd = cfg.get("client_metadata_url") or CLIENT_METADATA_URL
    if cimd and meta.get("client_id_metadata_document_supported"):
        return {"client_id": cimd, "client_secret": None}
    return None


async def _callback_server(port: int, state: str):
    """Loopback server on 127.0.0.1 (+ ::1 best-effort, for WSL/Windows browsers resolving localhost to IPv6).
    Returns (servers, future resolving to the auth code)."""
    code = asyncio.get_running_loop().create_future()

    async def handle(reader, writer):
        try:
            line = (await reader.readline()).decode("latin-1")
            q = {k: v[0] for k, v in parse_qs(urlparse(line.split(" ")[1]).query).items()}
        except Exception:
            q = {}
        ok = "code" in q and q.get("state") == state
        msg = "Authenticated. You can close this tab." if ok else f"Auth failed: {q.get('error', 'bad callback')}"
        writer.write(f"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nConnection: close\r\n\r\n{msg}".encode())
        await writer.drain()
        writer.close()
        if code.done() or not q:
            return
        if ok:
            code.set_result(q["code"])
        else:
            code.set_exception(RuntimeError(f"OAuth callback error: {q.get('error', 'state mismatch')}"))

    servers = [await asyncio.start_server(handle, "127.0.0.1", port)]
    port = servers[0].sockets[0].getsockname()[1]
    try:
        servers.append(await asyncio.start_server(handle, "::1", port))
    except OSError:
        pass
    return servers, code


async def _token_request(http: httpx.AsyncClient, meta: dict, client: dict, params: dict) -> dict:
    params = {**params, "client_id": client["client_id"]}
    auth = None
    if secret := client.get("client_secret"):
        methods = meta.get("token_endpoint_auth_methods_supported") or []
        if "client_secret_basic" in methods and "client_secret_post" not in methods:
            auth = (client["client_id"], secret)
        else:
            params["client_secret"] = secret
    r = await http.post(meta["token_endpoint"], data=params, auth=auth, headers={"Accept": "application/json"})
    if r.status_code >= 400:
        raise RuntimeError(f"token request failed ({r.status_code}): {r.text[:300]}")
    return r.json()


def _store_tokens(key: str, tok: dict, **extra) -> dict:
    exp = time.time() + tok["expires_in"] if tok.get("expires_in") else 0
    prev = _load().get(key, {})
    return _save(key, access_token=tok["access_token"], expires_at=exp,
                 refresh_token=tok.get("refresh_token") or prev.get("refresh_token"), **extra)


async def login(server_url: str, cfg: dict | None = None) -> dict:
    """Interactive authorization-code + PKCE flow; opens the browser (or announces the URL), returns the stored entry."""
    cfg = cfg or {}
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as http:
        meta = await _discover(http, server_url)
        scope = cfg.get("scope") or meta["_scope"]
        resource = meta["_resource"]
        stored = _load().get(server_url, {})

        # Reuse the registered port when free so a stored client_id's redirect_uri still matches.
        state = secrets.token_urlsafe(24)
        want = cfg.get("callback_port") or urlparse(stored.get("redirect_uri", "")).port or 0
        servers = None
        for port in dict.fromkeys((want, 0)):
            try:
                servers, code_fut = await _callback_server(port, state)
                break
            except OSError:
                continue
        if servers is None:
            raise RuntimeError("could not bind a loopback port for the OAuth callback")
        port = servers[0].sockets[0].getsockname()[1]
        redirect_uri = f"http://localhost:{port}/callback"

        try:
            client = _pick_client(meta, cfg, stored, redirect_uri) or await _register(http, meta, redirect_uri, scope)
            verifier = secrets.token_urlsafe(64)
            challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
            q = {"response_type": "code", "client_id": client["client_id"], "redirect_uri": redirect_uri,
                 "state": state, "code_challenge": challenge, "code_challenge_method": "S256",
                 "resource": resource}
            if scope:
                q["scope"] = scope
            auth_url = f"{meta['authorization_endpoint']}?{urlencode(q)}"
            opened = _open_browser(auth_url)
            copied = not opened and _copy(auth_url)
            _announce(f"MCP login for {server_url} — "
                      + ("browser opened; if nothing appeared, open this URL: " if opened
                         else "open this URL in a browser on this machine to authorize"
                         + (" (copied to clipboard): " if copied else ": "))
                      + auth_url)
            try:
                code = await asyncio.wait_for(code_fut, LOGIN_TIMEOUT)
            except TimeoutError:
                raise RuntimeError(f"OAuth login not completed within {LOGIN_TIMEOUT}s") from None
        finally:
            for s in servers:
                s.close()

        tok = await _token_request(http, meta, client, {
            "grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri,
            "code_verifier": verifier, "resource": resource})
        return _store_tokens(server_url, tok, **client, redirect_uri=redirect_uri,
                             token_endpoint=meta["token_endpoint"], scope=scope, resource=resource,
                             token_endpoint_auth_methods_supported=meta.get("token_endpoint_auth_methods_supported"))


async def _refresh(server_url: str, entry: dict) -> dict | None:
    """Refresh grant; None when it fails so the caller can fall back to login."""
    if not (entry.get("refresh_token") and entry.get("token_endpoint")):
        return None
    try:
        async with httpx.AsyncClient(timeout=30) as http:
            tok = await _token_request(http, entry, entry, {
                "grant_type": "refresh_token", "refresh_token": entry["refresh_token"],
                "resource": entry.get("resource") or server_url})
        return _store_tokens(server_url, tok)
    except Exception:
        return None


async def oauth_headers(server_url: str, cfg: dict | None = None, rejected: str | None = None) -> dict:
    """Bearer header from stored tokens: refresh if near expiry (or `rejected` by the server), else log in.
    Serialized per server so concurrent calls share one refresh/login."""
    async with _locks.setdefault(server_url, asyncio.Lock()):
        entry = _load().get(server_url, {})
        token = entry.get("access_token")
        stale = rejected is not None and rejected == f"Bearer {token}"
        fresh = token and not stale and (not entry.get("expires_at") or entry["expires_at"] - time.time() > REFRESH_SKEW)
        if not fresh:
            entry = await _refresh(server_url, entry) or await login(server_url, cfg)
        return {"Authorization": f"Bearer {entry['access_token']}"}
