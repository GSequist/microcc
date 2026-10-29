"""Client-side MCP tool resolution for all backends. Auth stays local: servers are called
directly, never through a model provider's connector.
Flow: tools/list -> Anthropic tool schemas -> tools/call on use. Remote: streamable HTTP first, SSE
fallback (SSE first for `"type": "sse"`). Local: stdio, one long-lived process per server.
"""

import asyncio
from contextlib import asynccontextmanager
import json
import os
import re
from pathlib import Path

from dotenv import dotenv_values


# Cache: server key (url, or stdio:<name>) -> {tools: [...anthropic format], routing: {name: {"url", "headers", "tool"}}}
_tool_cache = {}
# stdio key -> {"ready": future(session), "stop": Event, "task": Task, "loop": loop}
_stdio = {}

STDIO_LOG_DIR = Path.home() / ".micro-cc" / "mcp-logs"
_VAR = re.compile(r"\$\{(\w+)(?::-([^}]*))?\}")


def _fresh_env(project_dir: str = "") -> dict:
    """os.environ overlaid with the current .env files (global, then project), so a refreshed token
    is picked up without a restart."""
    env = dict(os.environ)
    env.update({k: v for k, v in dotenv_values(Path.home() / ".micro-cc" / ".env").items() if v})
    if project_dir:
        env.update({k: v for k, v in dotenv_values(Path(project_dir) / ".env").items() if v})
    return env


def _expand(template: str, env: dict) -> str | None:
    """Expand ${VAR} / ${VAR:-default}; None if a var without default is unset."""
    missing = [m.group(1) for m in _VAR.finditer(template) if not env.get(m.group(1)) and m.group(2) is None]
    if missing:
        return None
    return _VAR.sub(lambda m: env.get(m.group(1)) or m.group(2) or "", template)


def _expand_strict(template: str, env: dict) -> str:
    out = _expand(str(template), env)
    if out is None:
        unset = [m.group(1) for m in _VAR.finditer(str(template)) if not env.get(m.group(1))]
        raise RuntimeError(f"env var(s) not set: {', '.join(unset)} (add to ~/.micro-cc/.env or the project .env)")
    return out


def _env_headers(headers_env: dict, env: dict, literal: bool = False) -> dict:
    """{header: "VAR" | "Bearer ${VAR}"} -> real headers; headers with a missing var are skipped.
    With `literal`, a value without ${...} is used as-is instead of as a var name."""
    out = {}
    for name, spec in (headers_env or {}).items():
        spec = str(spec)
        template = spec if (literal or "${" in spec) else "${" + spec + "}"
        if (value := _expand(template, env)) is not None:
            out[name] = value
    return out


def _is_stdio(server: dict) -> bool:
    return server.get("type") == "stdio" or "command" in server


def _server_key(server: dict) -> str:
    return f"stdio:{server.get('name') or server['command']}" if _is_stdio(server) else server["url"]


def _has_static_auth(entry: dict) -> bool:
    return bool(entry.get("auth_env") or entry.get("headers_env") or entry.get("headers"))


async def _auth_headers(entry: dict, project_dir: str = "", rejected: str | None = None) -> dict:
    """Merge literal `headers`, OAuth bearer, `auth_env` bearer and `headers_env` (later wins); `rejected`
    is a bearer the server just refused, forcing an OAuth refresh/re-login. Credentials stay local."""
    if _is_stdio(entry["server"]):
        return {}
    env = _fresh_env(project_dir)
    headers = _env_headers(entry.get("headers"), env, literal=True)
    if oauth := entry.get("oauth"):
        from micro_cc.tools.mcp_oauth import oauth_headers
        headers.update(await oauth_headers(entry["server"]["url"], oauth if isinstance(oauth, dict) else None, rejected))
    if (auth_env := entry.get("auth_env")) and (token := env.get(auth_env)):
        headers["Authorization"] = f"Bearer {token}"
    headers.update(_env_headers(entry.get("headers_env"), env))
    return headers


def _entry(name: str, server: dict, top: dict) -> dict | None:
    """One normalized catalog entry; None if the spec is disabled or has neither url nor command."""
    if server.get("disabled") or top.get("disabled"):
        return None
    server = dict(server)
    kind = str(server.get("type") or server.get("transport") or "").lower()
    if "command" in server:
        server["type"] = "stdio"
    elif "url" not in server:
        return None
    elif kind == "sse":
        server["type"] = "sse"
    else:
        server.setdefault("type", "url")
    server.setdefault("name", name)
    pick = lambda k: server.get(k) if server.get(k) is not None else top.get(k)
    entry = {"server": server, "search_text": pick("search_text") or pick("description") or name}
    for k in ("auth_env", "oauth", "headers_env", "headers", "timeout"):
        entry[k] = pick(k)
    return entry


def normalize_config(data: dict, default_name: str) -> dict:
    """Any supported mcp.json shape -> {name: entry}: native {"server": {...}, "search_text", auth...},
    {"mcpServers"|"servers": {name: spec}} (Claude Desktop/Code, VS Code), or a bare {"url"|"command", ...}."""
    if not isinstance(data, dict):
        return {}
    if isinstance(data.get("server"), dict):
        specs = {data["server"].get("name") or default_name: data["server"]}
    elif isinstance(servers := data.get("mcpServers") or data.get("servers"), dict):
        specs = servers
    elif "url" in data or "command" in data:
        specs = {data.get("name") or default_name: data}
    else:
        return {}
    top = {k: v for k, v in data.items() if k not in ("server", "mcpServers", "servers")}
    out = {}
    for name, spec in specs.items():
        if isinstance(spec, dict) and (e := _entry(name, spec, top)):
            out[e["server"]["name"]] = e
    return out


CALL_TIMEOUT = 120   # connect + tool call
LIST_TIMEOUT = 30    # connect + tools/list (stdio servers get STDIO_LIST_TIMEOUT: npx/uvx cold starts)
STDIO_LIST_TIMEOUT = 90


async def _guarded(coro, timeout: float):
    """Await with a timeout; a CancelledError leaked by an inner anyio scope (dead server) becomes
    a RuntimeError, while a real outer cancel still propagates."""
    try:
        return await asyncio.wait_for(coro, timeout)
    except TimeoutError:
        raise RuntimeError(f"timed out after {timeout:g}s (server unreachable or unresponsive)") from None
    except asyncio.CancelledError:
        task = asyncio.current_task()
        if task and task.cancelling():
            raise
        raise RuntimeError("connection cancelled (server unreachable)") from None


def _auth_failed(e: BaseException, _seen: set = None, strict: bool = False) -> bool:
    """True if e, its cause chain or any grouped sub-exception looks like an auth rejection
    (`strict`: only 401-style, not 403/expired)."""
    _seen = _seen if _seen is not None else set()
    if e is None or id(e) in _seen:
        return False
    _seen.add(id(e))
    words = ("401", "unauthor", "invalid_token") if strict else ("401", "403", "unauthor", "expired", "forbidden", "invalid_token")
    if any(w in str(e).lower() for w in words):
        return True
    subs = [e.__cause__, e.__context__, *getattr(e, "exceptions", ())]
    return any(_auth_failed(x, _seen, strict) for x in subs)


def _leaf(e: BaseException) -> BaseException:
    """Innermost meaningful exception (unwraps groups and causes) for readable error messages."""
    seen = set()
    while id(e) not in seen:
        seen.add(id(e))
        nxt = (getattr(e, "exceptions", None) or [None])[0] or e.__cause__
        if nxt is None:
            break
        e = nxt
    return e


@asynccontextmanager
async def _streamable_streams(server_url: str, headers: dict):
    """Streamable-HTTP transport; supports mcp 1.x and 2.x (2.x renamed the client, dropped the
    headers kwarg, and changed the yielded tuple)."""
    import mcp.client.streamable_http as sh

    new = getattr(sh, "streamable_http_client", None)
    if new is not None:
        client = sh.create_mcp_http_client(headers=headers or None)
        try:
            async with new(server_url, http_client=client) as (read, write):
                yield read, write
        finally:
            await client.aclose()
        return

    old = getattr(sh, "streamablehttp_client", None)
    if old is None:
        raise ImportError(
            "installed `mcp` exposes neither streamable_http_client nor "
            "streamablehttp_client — no streamable-HTTP transport available"
        )
    async with old(server_url, headers=headers or None) as (read, write, _):
        yield read, write


@asynccontextmanager
async def _sse_streams(server_url: str, headers: dict):
    """SSE transport, the older protocol; same signature in mcp 1.x and 2.x."""
    from mcp.client.sse import sse_client
    async with sse_client(server_url, headers=headers or None) as (read, write):
        yield read, write


@asynccontextmanager
async def _connect(server_url: str, headers: dict = None, sse_first: bool = False):
    """Connect via streamable HTTP, falling back to SSE (reversed with `sse_first`). Only setup is
    guarded; errors raised in the consumer body propagate."""
    from mcp import ClientSession

    headers = dict(headers or {})
    errors = []

    openers = (_sse_streams, _streamable_streams) if sse_first else (_streamable_streams, _sse_streams)
    for opener in openers:
        opened = []
        try:
            transport = opener(server_url, headers)
            read, write = await transport.__aenter__()
            opened.append(transport)
            session_cm = ClientSession(read, write)
            session = await session_cm.__aenter__()
            opened.append(session_cm)
            await session.initialize()
        except (Exception, asyncio.CancelledError) as e:   # dead server: anyio cancels the host task
            errors.append(e)
            for cm in reversed(opened):   # unwind only what actually opened
                try:
                    await cm.__aexit__(type(e), e, e.__traceback__)
                except BaseException:
                    pass
            task = asyncio.current_task()
            if isinstance(e, asyncio.CancelledError) and task and task.cancelling():
                raise   # a real outer cancel, not the transport's own
            continue
        try:
            yield session
        finally:
            for cm in reversed(opened):
                try:
                    await cm.__aexit__(None, None, None)
                except Exception:
                    pass
        return

    # Surface an auth rejection over the fallback's error so callers can re-auth.
    cause = next((x for x in errors if _auth_failed(x)), errors[-1] if errors else None)
    raise RuntimeError(
        f"could not connect to MCP server {server_url} over streamable http or sse: {_leaf(cause) if cause else '?'}"
    ) from cause


async def _hold_stdio(key: str, server: dict, project_dir: str, slot: dict):
    """Owner task of a stdio server: spawn, initialize, publish the session, keep it open until stopped."""
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    STDIO_LOG_DIR.mkdir(parents=True, exist_ok=True)
    log = STDIO_LOG_DIR / f"{re.sub(r'[^\w.-]', '_', key.removeprefix('stdio:'))}.log"
    try:
        env = _fresh_env(project_dir)
        cwd = server.get("cwd")
        params = StdioServerParameters(
            command=_expand_strict(server["command"], env),
            args=[_expand_strict(a, env) for a in server.get("args") or []],
            env={k: _expand_strict(v, env) for k, v in (server.get("env") or {}).items()} or None,
            cwd=str(Path(project_dir or ".", cwd)) if cwd else (project_dir or None),
        )
        with open(log, "a", encoding="utf-8") as errlog:   # never inherit stderr: it would paint over the TUI
            async with stdio_client(params, errlog=errlog) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    slot["ready"].set_result(session)
                    await slot["stop"].wait()
    except BaseException as e:
        if not slot["ready"].done():
            slot["ready"].set_exception(RuntimeError(f"stdio server failed to start: {_leaf(e)} (stderr log: {log})"))
        if isinstance(e, asyncio.CancelledError):
            raise
    finally:
        if _stdio.get(key) is slot:
            _stdio.pop(key)


async def _stdio_session(key: str, server: dict, project_dir: str):
    """Shared session for a stdio server, (re)spawned on first use or after the process died."""
    loop = asyncio.get_running_loop()
    slot = _stdio.get(key)
    if not slot or slot["loop"] is not loop or slot["task"].done():
        slot = {"ready": loop.create_future(), "stop": asyncio.Event(), "loop": loop}
        slot["task"] = loop.create_task(_hold_stdio(key, server, project_dir, slot))
        _stdio[key] = slot
    return await asyncio.shield(slot["ready"])


@asynccontextmanager
async def _session(key: str, headers: dict, entry: dict | None, project_dir: str):
    """MCP session for a catalog entry: pooled for stdio, per-use connection for remote servers."""
    server = (entry or {}).get("server") or {}
    if _is_stdio(server):
        yield await _stdio_session(key, server, project_dir)
        return
    url = server.get("url") or key
    if "${" in url:
        url = _expand_strict(url, _fresh_env(project_dir))
    async with _connect(url, headers=headers, sse_first=server.get("type") == "sse") as session:
        yield session


def _safe_name(name: str) -> str:
    """Provider tool names must match ^[a-zA-Z0-9_-]{1,64}$."""
    return re.sub(r"[^a-zA-Z0-9_-]", "_", name)[:64] or "tool"


def _mcp_tool_to_anthropic(t) -> dict:
    """Convert an MCP Tool to Anthropic tool schema; reads `input_schema` (2.x) or `inputSchema` (1.x)."""
    raw = getattr(t, "input_schema", None)
    if raw is None:
        raw = getattr(t, "inputSchema", None)
    schema = dict(raw or {})
    if "type" not in schema:
        schema["type"] = "object"
    if "properties" not in schema:
        schema["properties"] = {}
    return {
        "name": _safe_name(t.name),
        "description": t.description or "",
        "input_schema": schema,
    }


async def fetch_mcp_tools(server_url: str, headers: dict = None, entry: dict = None,
                          project_dir: str = "") -> tuple[list[dict], dict[str, dict]]:
    """Connect to MCP server, return tools as Anthropic tool schemas.

    Returns:
        (anthropic_tools, mcp_routing)
        - anthropic_tools: list of {"name", "description", "input_schema"} dicts
        - mcp_routing: {tool_name: {"url": server key, "headers": headers, "tool": original MCP name}}
    """
    if server_url in _tool_cache:
        cached = _tool_cache[server_url]
        return cached["tools"], cached["routing"]

    anthropic_tools = []
    routing = {}

    async with _session(server_url, headers, entry, project_dir) as session:
        result = await session.list_tools()
        for t in result.tools:
            schema = _mcp_tool_to_anthropic(t)
            anthropic_tools.append(schema)
            routing[schema["name"]] = {"url": server_url, "headers": headers, "tool": t.name}

    _tool_cache[server_url] = {"tools": anthropic_tools, "routing": routing}
    return anthropic_tools, routing


def _format_result(result) -> str:
    """CallToolResult -> text: text/resource/binary blocks, structured content fallback, server-flagged errors."""
    parts = []
    for block in (result.content or []):
        if getattr(block, "text", None) is not None:
            parts.append(block.text)
        elif (res := getattr(block, "resource", None)) is not None:
            parts.append(getattr(res, "text", None) or f"[resource: {getattr(res, 'uri', '')}]")
        elif hasattr(block, "data"):
            mime = getattr(block, "mime_type", None) or getattr(block, "mimeType", "")
            parts.append(f"[binary data: {mime}]")
        elif getattr(block, "uri", None):
            parts.append(f"[resource link: {block.uri}]")
        else:
            parts.append(str(block))
    structured = getattr(result, "structured_content", None) or getattr(result, "structuredContent", None)
    if not parts and structured is not None:
        parts.append(json.dumps(structured, ensure_ascii=False))
    out = "\n".join(parts) if parts else "OK"
    if getattr(result, "is_error", None) or getattr(result, "isError", None):
        return f"MCP tool error (reported by server): {out}"
    return out


async def call_mcp_tool(server_url: str, tool_name: str, arguments: dict, headers: dict = None,
                        entry: dict = None, project_dir: str = "") -> str:
    """Execute a tool call on the MCP server. Returns result as string. With `entry`, auth headers are
    re-resolved per call so refreshed tokens apply without reloading the server; an OAuth bearer the
    server rejects triggers one refresh/re-login and a retry."""
    async def _call(h):
        async with _session(server_url, h, entry, project_dir) as session:
            return _format_result(await session.call_tool(tool_name, arguments))

    timeout = (entry or {}).get("timeout") or CALL_TIMEOUT
    try:
        if entry:
            headers = await _auth_headers(entry, project_dir) or None   # may block on OAuth login (own timeout)
        try:
            return await _guarded(_call(headers), timeout)
        except Exception as e:
            if not (entry and entry.get("oauth") and _auth_failed(e)):
                raise
            headers = await _auth_headers(entry, project_dir, rejected=(headers or {}).get("Authorization")) or None
            return await _guarded(_call(headers), timeout)
    except Exception as e:
        hint = ""
        if entry and _auth_failed(e):
            hint = (" — auth rejected (token likely expired). Tokens are re-read from .env / the OAuth store on every "
                    "call, so no reload is needed: refresh the token (update the env var in ~/.micro-cc/.env or the "
                    "project .env; OAuth servers refresh or re-login automatically) and retry the call.")
        return f"MCP tool error: {e}{hint}"


async def _list_tools(entry: dict, project_dir: str) -> tuple[list[dict], dict[str, dict]]:
    """tools/list with auth; a 401 on a server with no auth configured falls back to OAuth (mcpServers-style
    configs never say `"oauth": true`); `"oauth": false` opts out."""
    server = entry["server"]
    key = _server_key(server)
    timeout = STDIO_LIST_TIMEOUT if _is_stdio(server) else LIST_TIMEOUT
    headers = await _auth_headers(entry, project_dir)
    try:
        return await _guarded(fetch_mcp_tools(key, headers or None, entry, project_dir), timeout)
    except Exception as e:
        if _is_stdio(server) or not _auth_failed(e, strict=not entry.get("oauth")):
            raise
        if entry.get("oauth") is None and not _has_static_auth(entry):
            entry["oauth"] = True
        elif not entry.get("oauth"):
            raise
        try:
            headers = await _auth_headers(entry, project_dir, rejected=headers.get("Authorization"))
        except Exception as auth_e:
            raise RuntimeError(f"{e} (OAuth login failed: {auth_e})") from e
        return await _guarded(fetch_mcp_tools(key, headers or None, entry, project_dir), timeout)


async def resolve_mcp_tools(mcp_catalog_entries: list[dict], project_dir: str = "") -> tuple[list[dict], dict[str, dict], list[str]]:
    """Fetch tools for each catalog entry: `server` (url or stdio command) plus optional `auth_env`, `oauth`,
    `headers_env`, `headers`. Returns (all_anthropic_tools, routing_map, failures). Failures are strings for
    the caller to surface; never print them here (it lands in the TUI alt screen)."""
    all_tools = []
    all_routing = {}
    failures = []

    for entry in mcp_catalog_entries:
        server = entry["server"]
        name = server.get("name") or _server_key(server)
        try:
            tools, routing = await _list_tools(entry, project_dir)
            for r in routing.values():
                r["entry"] = entry
            all_tools.extend(tools)
            all_routing.update(routing)
        except Exception as e:
            failures.append(f"MCP server '{name}' unreachable: {e}")

    return all_tools, all_routing, failures
