"""Client-side MCP tool resolution for all backends. Auth stays local: servers are called
directly, never through a model provider's connector.
Flow: tools/list -> Anthropic tool schemas -> tools/call on use. Streamable HTTP first, SSE fallback.
"""

from contextlib import asynccontextmanager
import os


# Cache: server_url -> {tools: [...anthropic format], routing: {name: {"url", "headers"}}}
_tool_cache = {}


def _auth_headers(entry: dict) -> dict:
    """Bearer header from the env var named in `auth_env`; credentials stay local."""
    auth_env = entry.get("auth_env")
    if not auth_env:
        return {}
    token = os.environ.get(auth_env)
    return {"Authorization": f"Bearer {token}"} if token else {}


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
async def _connect(server_url: str, headers: dict = None):
    """Connect via streamable HTTP, falling back to SSE. Only setup is guarded;
    errors raised in the consumer body propagate."""
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
    """Fetch tools for each catalog entry: `server` plus optional `auth_env` (bearer token env var).
    Returns (all_anthropic_tools, routing_map, failures). Failures are strings for the caller to
    surface; never print them here (it lands in the TUI alt screen)."""
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
