import asyncio
import base64
import hashlib
import http.server
import json
import os
import secrets
import threading
import time
import urllib.parse
import webbrowser

import httpx

# Public OAuth client id for Anthropic's official Claude Code CLI login
# flow. Not a secret — OAuth "public client" pattern, RFC 8252 — the
# per-login secret is the PKCE verifier generated fresh below, never this
# fixed id.
_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
_AUTHORIZE_URL = "https://claude.ai/oauth/authorize"
_TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
_CALLBACK_PORT = 53692
_REDIRECT_URI = f"http://localhost:{_CALLBACK_PORT}/callback"
_SCOPE = "org:create_api_key user:profile user:inference"
# How long to wait on the loopback socket for the browser redirect before
# giving up — otherwise a closed tab / abandoned login hangs the worker
# (and the TUI's exclusive worker slot) forever.
_CALLBACK_TIMEOUT_S = 300


def discover_claude_code_token() -> str | None:
    if token := os.getenv("CLAUDE_CODE_OAUTH_TOKEN"):
        return token
    creds_path = os.path.expanduser("~/.claude/.credentials.json")
    if os.path.exists(creds_path):
        data = json.load(open(creds_path))
        return data.get("accessToken") or data.get("access_token")
    return None


def is_oauth_token(token: str) -> bool:
    # sk-ant-oat*/cc- prefixes → OAuth; sk-ant-api* → real API key.
    return token.startswith(("sk-ant-oat", "cc-"))


def _generate_pkce() -> tuple[str, str]:
    """RFC 7636. verifier is the actual per-login secret (32 random bytes,
    generated fresh every attempt, never shipped anywhere); challenge is its
    SHA-256 hash, the only part sent before the token exchange."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b"=").decode()
    return verifier, challenge


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        self.server.result = {
            "code": params.get("code", [None])[0],
            "state": params.get("state", [None])[0],
        }
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(b"<html><body>Logged in \xe2\x80\x94 you can close this tab.</body></html>")

    def log_message(self, *args):
        pass  # don't spam the TUI's stdout with HTTP access logs


def _wait_for_callback(cancel_event: threading.Event) -> dict | None:
    """Blocking (real blocking socket I/O, not a coroutine) — run this via
    asyncio.to_thread, not awaited directly.

    Polls handle_request() in short (1s) increments instead of one blocking
    call with the full timeout — asyncio Task cancellation only stops the
    coroutine that's AWAITING this thread, it can't interrupt a blocking
    socket call already running inside the thread itself. Without polling,
    cancelling the caller (e.g. Esc in the TUI) would abandon the await but
    leave this thread holding the port for up to _CALLBACK_TIMEOUT_S
    regardless — exactly what left a stale listener on _CALLBACK_PORT after
    an escaped login attempt, breaking the next one. Checking cancel_event
    every ~1s bounds how long that abandonment can last, and server_close()
    in `finally` guarantees the port is freed on every exit path."""
    server = http.server.HTTPServer(("127.0.0.1", _CALLBACK_PORT), _CallbackHandler)
    server.timeout = 1  # poll interval, not the overall deadline
    server.result = None
    deadline = time.monotonic() + _CALLBACK_TIMEOUT_S
    try:
        while time.monotonic() < deadline:
            if cancel_event.is_set():
                return None
            server.handle_request()  # returns after `timeout` even with no request
            if server.result is not None:
                return server.result
        return None
    finally:
        server.server_close()


async def oauth_login_flow(cancel_event: threading.Event | None = None) -> str:
    """Fallback OAuth flow: only runs if discover_claude_code_token() found
    nothing. Uses the same client id, param names, and reuse of the PKCE
    verifier as the OAuth `state` that Anthropic's own official CLI does,
    rather than improvising a shape that might silently be rejected by
    Anthropic's endpoint.

    cancel_event: set it from the caller to abort the wait early (see
    _wait_for_callback) — a caller that never cancels can just omit it."""
    if cancel_event is None:
        cancel_event = threading.Event()
    verifier, challenge = _generate_pkce()

    auth_params = urllib.parse.urlencode({
        "code": "true",
        "client_id": _CLIENT_ID,
        "response_type": "code",
        "redirect_uri": _REDIRECT_URI,
        "scope": _SCOPE,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": verifier,
    })
    webbrowser.open(f"{_AUTHORIZE_URL}?{auth_params}")

    result = await asyncio.to_thread(_wait_for_callback, cancel_event)
    if result is None or result["code"] is None:
        raise RuntimeError("Claude login timed out or was cancelled — no code received")
    if result["state"] != verifier:
        raise RuntimeError("OAuth state mismatch — possible interception, aborting")

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            _TOKEN_URL,
            json={
                "grant_type": "authorization_code",
                "client_id": _CLIENT_ID,
                "code": result["code"],
                "state": result["state"],
                "redirect_uri": _REDIRECT_URI,
                "code_verifier": verifier,
            },
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        resp.raise_for_status()
        return resp.json()["access_token"]
