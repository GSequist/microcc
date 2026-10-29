"""MCP client + OAuth: config shapes, env expansion, a real stdio server (pooled session, sanitized names,
server-flagged errors), OAuth discovery/client choice/re-auth over httpx.MockTransport, the auth-notice pump."""

import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
import textwrap
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
os.environ["HOME"] = tempfile.mkdtemp(prefix="mcc-home-")
os.environ["MICRO_CC_POSTGRES_URL"] = ""

import httpx  # noqa: E402

from micro_cc.tools import mcp_client_ as mc, mcp_oauth as mo  # noqa: E402
from micro_cc.tools.search_tool_ import _scan_mcps_dir  # noqa: E402

SERVER = textwrap.dedent("""
    import os
    from mcp.server.mcpserver import MCPServer
    app = MCPServer("t")

    @app.tool(name="echo.v2")
    def echo(text: str) -> str:
        return f"{text}|{os.getpid()}|{os.environ.get('GREETING', '')}"

    @app.tool()
    def boom() -> str:
        raise ValueError("kaboom")

    app.run()
""")


class ConfigShapes(unittest.TestCase):
    def test_native_shape_back_compat(self):
        data = {"server": {"type": "url", "url": "https://x/mcp", "name": "x"}, "search_text": "xs",
                "auth_env": "TOK", "oauth": True, "headers_env": {"H": "V"}, "timeout": 5}
        e = mc.normalize_config(data, "dir")["x"]
        self.assertEqual(e["server"], {"type": "url", "url": "https://x/mcp", "name": "x"})
        self.assertEqual((e["search_text"], e["auth_env"], e["oauth"], e["headers_env"], e["timeout"]),
                         ("xs", "TOK", True, {"H": "V"}, 5))

    def test_native_without_name_uses_dir(self):
        self.assertIn("dir", mc.normalize_config({"server": {"url": "https://x"}}, "dir"))

    def test_mcpservers_shape(self):
        data = {"mcpServers": {
            "fs": {"command": "npx", "args": ["-y", "fs"], "env": {"A": "b"}},
            "remote": {"type": "http", "url": "https://r/mcp", "headers": {"X-Key": "${K}"}},
            "legacy": {"type": "sse", "url": "https://l/sse"},
            "off": {"url": "https://o", "disabled": True},
            "broken": {"type": "http"}}}
        cat = mc.normalize_config(data, "f")
        self.assertEqual(sorted(cat), ["fs", "legacy", "remote"])
        self.assertEqual(cat["fs"]["server"]["type"], "stdio")
        self.assertEqual(cat["legacy"]["server"]["type"], "sse")
        self.assertEqual(cat["remote"]["headers"], {"X-Key": "${K}"})

    def test_vscode_and_bare(self):
        self.assertIn("a", mc.normalize_config({"servers": {"a": {"type": "stdio", "command": "x"}}}, "f"))
        self.assertEqual(mc.normalize_config({"url": "https://b"}, "b")["b"]["server"]["type"], "url")
        self.assertEqual(mc.normalize_config({"nope": 1}, "f"), {})

    def test_scan_dir_mixed(self):
        d = Path(tempfile.mkdtemp())
        (d / "one").mkdir()
        (d / "one" / "mcp.json").write_text(json.dumps({"server": {"url": "https://1"}, "search_text": "s"}))
        (d / "many.json").write_text(json.dumps({"mcpServers": {"p": {"url": "https://p"}, "q": {"command": "q"}}}))
        (d / "bad.json").write_text("{")
        self.assertEqual(sorted(_scan_mcps_dir(d)), ["one", "p", "q"])


class Expansion(unittest.TestCase):
    def test_expand(self):
        env = {"A": "1"}
        self.assertEqual(mc._expand("x${A}y${B:-z}", env), "x1yz")
        self.assertIsNone(mc._expand("${B}", env))
        with self.assertRaises(RuntimeError):
            mc._expand_strict("${B}", env)

    def test_headers(self):
        env = {"T": "tok"}
        self.assertEqual(mc._env_headers({"A": "T", "B": "Bearer ${T}", "C": "MISSING"}, env),
                         {"A": "tok", "B": "Bearer tok"})
        self.assertEqual(mc._env_headers({"A": "plain", "B": "${MISSING}"}, env, literal=True), {"A": "plain"})


class Stdio(unittest.IsolatedAsyncioTestCase):
    async def test_stdio_roundtrip_pooled(self):
        script = Path(tempfile.mkdtemp()) / "srv.py"
        script.write_text(SERVER)
        entry = mc.normalize_config({"mcpServers": {"t": {
            "command": sys.executable, "args": [str(script)], "env": {"GREETING": "${G:-hi}"}}}}, "f")["t"]
        tools, routing, failures = await mc.resolve_mcp_tools([entry])
        self.assertEqual(failures, [])
        self.assertIn("echo_v2", routing)
        self.assertEqual(routing["echo_v2"]["tool"], "echo.v2")
        r = routing["echo_v2"]
        out1 = await mc.call_mcp_tool(r["url"], r["tool"], {"text": "a"}, entry=r["entry"])
        out2 = await mc.call_mcp_tool(r["url"], r["tool"], {"text": "b"}, entry=r["entry"])
        self.assertTrue(out1.startswith("a|") and out1.endswith("|hi"), out1)
        self.assertEqual(out1.split("|")[1], out2.split("|")[1])   # same process: pooled session
        err = await mc.call_mcp_tool(r["url"], "boom", {}, entry=r["entry"])
        self.assertTrue(err.startswith("MCP tool error (reported by server)"), err)
        slot = mc._stdio[r["url"]]
        slot["stop"].set()
        await slot["task"]
        self.assertNotIn(r["url"], mc._stdio)

    async def test_stdio_bad_command_reports(self):
        entry = mc.normalize_config({"command": "/nonexistent/bin"}, "nope")["nope"]
        _, _, failures = await mc.resolve_mcp_tools([entry])
        self.assertTrue(failures and "stdio server failed to start" in failures[0], failures)


def _as_transport(calls):
    def handler(req: httpx.Request):
        calls.append(req)
        u = str(req.url)
        if u == "https://srv/mcp":
            return httpx.Response(401, headers={"www-authenticate":
                'Bearer resource_metadata="https://srv/prm", scope="read write"'})
        if u == "https://srv/prm":
            return httpx.Response(200, json={"resource": "https://srv/", "authorization_servers": ["https://as"]})
        if u == "https://as/.well-known/oauth-authorization-server":
            return httpx.Response(200, json={"authorization_endpoint": "https://as/auth", "token_endpoint": "https://as/tok",
                                             "client_id_metadata_document_supported": True,
                                             "token_endpoint_auth_methods_supported": ["client_secret_basic"]})
        if u == "https://as/tok":
            return httpx.Response(200, json={"access_token": "new", "expires_in": 3600})
        return httpx.Response(404)
    return httpx.MockTransport(handler)


class OAuth(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        mo.STORE.unlink(missing_ok=True)

    async def test_discover_challenge_scope_and_resource(self):
        async with httpx.AsyncClient(transport=_as_transport([])) as http:
            meta = await mo._discover(http, "https://srv/mcp")
        self.assertEqual((meta["_scope"], meta["_resource"], meta["token_endpoint"]),
                         ("read write", "https://srv/", "https://as/tok"))

    def test_pick_client_order(self):
        meta = {"client_id_metadata_document_supported": True}
        redirect = "http://localhost:5/callback"
        self.assertEqual(mo._pick_client(meta, {"client_id": "pre"}, {}, redirect)["client_id"], "pre")
        self.assertEqual(mo._pick_client(meta, {}, {"client_id": "dcr", "redirect_uri": redirect}, redirect)["client_id"], "dcr")
        self.assertIsNone(mo._pick_client(meta, {}, {"client_id": "dcr", "redirect_uri": "http://localhost:6/callback"}, redirect))
        cimd = {"client_metadata_url": "https://me/cimd.json"}
        self.assertEqual(mo._pick_client(meta, cimd, {}, redirect)["client_id"], "https://me/cimd.json")
        self.assertIsNone(mo._pick_client({}, cimd, {}, redirect))   # AS doesn't advertise CIMD -> DCR
        self.assertEqual(mo._pick_client({}, {}, {"client_id": "https://me/cimd.json", "redirect_uri": "x"}, redirect)["client_id"],
                         "https://me/cimd.json")   # stored CIMD id survives a port change

    async def test_token_request_basic_auth(self):
        calls = []
        async with httpx.AsyncClient(transport=_as_transport(calls)) as http:
            await mo._token_request(http, {"token_endpoint": "https://as/tok",
                                           "token_endpoint_auth_methods_supported": ["client_secret_basic"]},
                                    {"client_id": "c", "client_secret": "s"}, {"grant_type": "x"})
        self.assertTrue(calls[-1].headers["authorization"].startswith("Basic "))
        self.assertNotIn(b"client_secret", calls[-1].content)

    async def test_rejected_token_refreshes_once(self):
        mo._save("https://srv/mcp", access_token="old", expires_at=time.time() + 3600, refresh_token="r",
                 token_endpoint="https://as/tok")   # legacy store entry: no `resource`
        refreshed = []

        async def fake_refresh(url, entry):
            refreshed.append(entry.get("resource") or url)
            return mo._store_tokens(url, {"access_token": "new"})

        with mock.patch.object(mo, "_refresh", fake_refresh):
            self.assertEqual(await mo.oauth_headers("https://srv/mcp"), {"Authorization": "Bearer old"})
            self.assertEqual(await mo.oauth_headers("https://srv/mcp", rejected="Bearer old"), {"Authorization": "Bearer new"})
            # a concurrent caller rejected with the already-replaced token doesn't refresh again
            self.assertEqual(await mo.oauth_headers("https://srv/mcp", rejected="Bearer old"), {"Authorization": "Bearer new"})
        self.assertEqual(refreshed, ["https://srv/mcp"])

    def test_wsl_without_opener_returns_false(self):
        with mock.patch.dict(os.environ, {"WSL_DISTRO_NAME": "Ubuntu"}), mock.patch.object(mo.shutil, "which", return_value=None):
            self.assertFalse(mo._open_browser("https://as/auth"))

    def test_headless_linux_skips_text_browser(self):
        env = {k: v for k, v in os.environ.items() if k not in ("DISPLAY", "WAYLAND_DISPLAY", "WSL_DISTRO_NAME")}
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(mo.sys, "platform", "linux"), \
                mock.patch.object(mo, "_is_wsl", return_value=False), mock.patch.object(mo.webbrowser, "open") as op:
            self.assertFalse(mo._open_browser("https://as/auth"))
            op.assert_not_called()


class Notices(unittest.IsolatedAsyncioTestCase):
    async def test_pump_yields_auth_url_and_result(self):
        from micro_cc.claude_loop_ import _with_auth_notices

        async def work():
            mo._announce("open https://as/auth")
            await asyncio.sleep(0.01)
            return 42

        out, events = [], []
        async for ev in _with_auth_notices(work(), out):
            events.append(ev)
        self.assertEqual(events, [{"type": "mcp_auth", "message": "open https://as/auth"}])
        self.assertEqual(out, [42])


if __name__ == "__main__":
    unittest.main()
