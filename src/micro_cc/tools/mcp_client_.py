"""Client-side MCP tool resolution — used for ALL backends.

Real 3rd-party MCP servers (Azure MCP, Graph MCP, etc.) are executed by us,
not proxied through a vendor's server-side MCP connector: their auth (API
keys, OAuth bearer tokens) is ours to hold, and should never transit a model
provider's infrastructure. So regardless of which model backend is active:
  1. Connect to MCP server → fetch tool schemas via tools/list
  2. Convert to Anthropic tool-schema format (the format tool_schemas already
     use everywhere in claude_loop_) → append into the normal tools array
  3. Route tool_use calls back to MCP server via tools/call

Uses the `mcp` package (pip install mcp).
Tries streamable HTTP first, falls back to SSE for older servers.
"""

from contextlib import asynccontextmanager
import os


# Cache: server_url -> {tools: [...anthropic format], routing: {name: {"url", "headers"}}}
_tool_cache = {}


def _auth_headers(entry: dict) -> dict:
    """Build request headers from an entry's `auth_env` (env var holding a
    bearer token) — keeps credentials local, never sent to a model provider."""
    auth_env = entry.get("auth_env")
    if not auth_env:
        return {}
    token = os.environ.get(auth_env)
    return {"Authorization": f"Bearer {token}"} if token else {}


@asynccontextmanager
async def _streamable_streams(server_url: str, headers: dict):
    """Streamable-HTTP transport, across both `mcp` API generations.

    `mcp` 2.x renamed `streamablehttp_client` -> `streamable_http_client`,
    dropped its `headers=` kwarg (headers now ride on a preconfigured httpx
    client), and shrank the yielded tuple from `(read, write, _)` to
    `(read, write)`. pyproject's floor still allows 1.x, so support both
    rather than pinning — an upstream rename must not take every MCP server
    down with it, which is exactly what happened here.
    """
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
    """SSE transport — the older protocol, which servers like Manifold still
    speak. Signature is unchanged across the 1.x -> 2.x rename."""
    from mcp.client.sse import sse_client
    async with sse_client(server_url, headers=headers or None) as (read, write):
        yield read, write


@asynccontextmanager
async def _connect(server_url: str, headers: dict = None):
    """Connect to an MCP server: streamable HTTP first, SSE as fallback.

    `mcp` is imported lazily inside each transport, not at module level —
    importing the bare package drags in its whole server-side stack
    (fastmcp, uvicorn, starlette) even though only the client bits are used,
    and this module is imported eagerly at app startup whether or not any
    MCP server is ever touched this session.

    Only SETUP is guarded per transport: a transport that fails to open (a
    missing import, a refused connection, a server that doesn't speak that
    protocol) falls through to the next. The consumer's body is deliberately
    NOT guarded — a tool call that raises must propagate. The previous shape
    wrapped the `yield` in `try/except: pass`, so a consumer error silently
    re-connected over the next transport instead of surfacing.
    """
    from mcp import ClientSession

    headers = dict(headers or {})
    last_exc = None

    for opener in (_streamable_streams, _sse_streams):
        opened = []
        try:
            transport = opener(server_url, headers)
            read, write = await transport.__aenter__()
            opened.append(transport)
            session_cm = ClientSession(read, write)
            session = await session_cm.__aenter__()
            opened.append(session_cm)
            await session.initialize()
        except Exception as e:
            last_exc = e
            for cm in reversed(opened):   # unwind only what actually opened
                try:
                    await cm.__aexit__(type(e), e, e.__traceback__)
                except Exception:
                    pass
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

    raise RuntimeError(
        f"could not connect to MCP server {server_url} over streamable http or sse"
    ) from last_exc


def _mcp_tool_to_anthropic(t) -> dict:
    """Convert a single MCP Tool to Anthropic tool-schema format.

    Field name moved with the 2.x rename too: the Tool model now exposes
    `input_schema` (snake_case) where 1.x had `inputSchema`. Read both so
    the same build works against either.
    """
    raw = getattr(t, "input_schema", None)
    if raw is None:
        raw = getattr(t, "inputSchema", None)
    schema = dict(raw or {})
    if "type" not in schema:
        schema["type"] = "object"
    if "properties" not in schema:
        schema["properties"] = {}
    return {
        "name": t.name,
        "description": t.description or "",
        "input_schema": schema,
    }


async def fetch_mcp_tools(server_url: str, headers: dict = None) -> tuple[list[dict], dict[str, dict]]:
    """Connect to MCP server, return tools as Anthropic tool schemas.

    Returns:
        (anthropic_tools, mcp_routing)
        - anthropic_tools: list of {"name", "description", "input_schema"} dicts
        - mcp_routing: {tool_name: {"url": server_url, "headers": headers}}
    """
    if server_url in _tool_cache:
        cached = _tool_cache[server_url]
        return cached["tools"], cached["routing"]

    anthropic_tools = []
    routing = {}

    async with _connect(server_url, headers=headers) as session:
        result = await session.list_tools()
        for t in result.tools:
            anthropic_tools.append(_mcp_tool_to_anthropic(t))
            routing[t.name] = {"url": server_url, "headers": headers}

    _tool_cache[server_url] = {"tools": anthropic_tools, "routing": routing}
    return anthropic_tools, routing


async def call_mcp_tool(server_url: str, tool_name: str, arguments: dict, headers: dict = None) -> str:
    """Execute a tool call on the MCP server. Returns result as string."""
    try:
        async with _connect(server_url, headers=headers) as session:
            result = await session.call_tool(tool_name, arguments)

            parts = []
            for block in (result.content or []):
                if hasattr(block, "text"):
                    parts.append(block.text)
                elif hasattr(block, "data"):
                    mime = getattr(block, "mime_type", None) or getattr(block, "mimeType", "")
                    parts.append(f"[binary data: {mime}]")
                else:
                    parts.append(str(block))

            return "\n".join(parts) if parts else "OK"
    except Exception as e:
        return f"MCP tool error: {e}"


async def resolve_mcp_tools(mcp_catalog_entries: list[dict]) -> tuple[list[dict], dict[str, dict], list[str]]:
    """Given MCP_CATALOG entries, fetch all tools client-side.

    Args:
        mcp_catalog_entries: list of MCP_CATALOG values, each with a "server"
            key and an optional "auth_env" (env var name holding a bearer
            token for that server).

    Returns:
        (all_anthropic_tools, routing_map, failures) — failures is a list of
        human-readable strings, one per server that couldn't be reached, for
        the caller to surface (claude_loop_ yields them as "error" events —
        never printed here: this runs inside a TUI's alt screen, and a bare
        print() lands wherever the cursor happens to be instead of the
        transcript).
    """
    all_tools = []
    all_routing = {}
    failures = []

    for entry in mcp_catalog_entries:
        server = entry["server"]
        url = server["url"]
        name = server.get("name", url)
        headers = _auth_headers(entry)
        try:
            tools, routing = await fetch_mcp_tools(url, headers=headers or None)
            all_tools.extend(tools)
            all_routing.update(routing)
        except Exception as e:
            failures.append(f"MCP server '{name}' unreachable: {e}")

    return all_tools, all_routing, failures
