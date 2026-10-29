"""GUI surface: every /api route plus every tool the model can call through /api/chat.

Real: the FastAPI app, bridge, claude_loop and the tools themselves. Faked: the
model endpoint (scripted OpenAI-shaped SSE, LiteLLM route); HOME is a temp dir
and provider keys are pinned so nothing leaves the machine.
"""

import http.server
import json
import os
import socketserver
import sys
import tempfile
import threading
import unittest
from pathlib import Path

SRC = str(Path(__file__).resolve().parents[1] / "src")
sys.path.insert(0, SRC)

_TMP_HOME = tempfile.mkdtemp(prefix="mcc-home-")
_ROUTING_KEYS = [
    "OLLAMA_BASE_URL", "ANTHROPIC_API_KEY", "ANTHROPIC_OAUTH_TOKEN", "FOUNDRY_BASE_URL",
    "FOUNDRY_API_KEY", "LITELLM_BASE_URL", "LITELLM_API_KEY", "OPENROUTER_API_KEY",
    "OPENROUTER_BASE_URL", "OPENAI_BASE_URL", "OPENAI_API_KEY", "MICRO_CC_POSTGRES_URL",
    "MICROCC_PROVIDER",
]
_REAL_ENV = {k: os.environ.get(k) for k in ["HOME", *_ROUTING_KEYS]}
os.environ["HOME"] = _TMP_HOME
for _k in _ROUTING_KEYS:
    os.environ[_k] = ""

from fastapi.testclient import TestClient  # noqa: E402

from micro_cc.utils import settings_store_  # noqa: E402
from micro_cc.utils.msg_store_ import load_msgs, store_msgs  # noqa: E402
from micro_cc.webui import server, session  # noqa: E402

_saved = {}


def setUpModule():
    _saved.update(settings=settings_store_._SETTINGS_PATH)
    os.environ["HOME"] = _TMP_HOME
    settings_store_._SETTINGS_PATH = Path(_TMP_HOME) / ".micro-cc" / "settings.json"


def tearDownModule():
    for k, v in _REAL_ENV.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    settings_store_._SETTINGS_PATH = _saved["settings"]


class _Model(http.server.BaseHTTPRequestHandler):
    """Pops one step per request: {"tool": name, "args": {...}} or {"text": str}."""
    script: list = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        step = _Model.script.pop(0) if _Model.script else {"text": "(script exhausted)"}
        if "tool" in step:
            delta = {"role": "assistant", "tool_calls": [{
                "index": 0, "id": f"call_{len(step['tool'])}_{id(step) % 9973}", "type": "function",
                "function": {"name": step["tool"], "arguments": json.dumps(step["args"])},
            }]}
            chunks = [{"choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
                      {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]}]
        else:
            chunks = [{"choices": [{"index": 0, "delta": {"role": "assistant", "content": step["text"]}, "finish_reason": None}]},
                      {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}]
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for c in chunks:
            c.update({"id": "x", "object": "chat.completion.chunk", "created": 0, "model": "fake"})
            self.wfile.write(f"data: {json.dumps(c)}\n\n".encode())
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


def _events(resp) -> list:
    out = []
    for block in resp.text.split("\n\n"):
        block = block.strip()
        if block.startswith("data: ") and block != "data: [DONE]":
            out.append(json.loads(block[6:]))
    return out


def _user(text):
    return {"messages": [{"id": "u", "role": "user", "parts": [{"type": "text", "text": text}]}]}


class GuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _Model)
        cls.httpd.daemon_threads = True
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        os.environ["LITELLM_BASE_URL"] = f"http://127.0.0.1:{cls.httpd.server_address[1]}"
        os.environ["LITELLM_API_KEY"] = "test"

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        for k in ("LITELLM_BASE_URL", "LITELLM_API_KEY"):
            os.environ[k] = ""

    def setUp(self):
        self.proj = tempfile.mkdtemp(prefix="mcc-gui-")
        session.init(self.proj, msgs=[], model="fake", trim_budget=100_000)
        settings_store_.edit_setting("model", "fake")
        settings_store_.edit_setting("dangerous", [])
        self.client = TestClient(server.app)
        self.client.__enter__()
        _Model.script = []

    def tearDown(self):
        self.client.__exit__(None, None, None)

    def chat(self, text, script):
        _Model.script = list(script)
        return self.client.post("/api/chat", json=_user(text))

    # ── session / settings ─────────────────────────────────────────

    def test_session_payload_and_command_palette(self):
        d = self.client.get("/api/session").json()
        self.assertEqual(d["session"]["project_dir"], self.proj)
        self.assertIn("bash_", d["session"]["gateable"])
        self.assertTrue(d["session"]["models"])
        names = {c["name"] for c in d["commands"]}
        self.assertNotIn("/exit", names)
        self.assertNotIn("/gui", names)
        self.assertIn("/clear", names)
        self.assertTrue(d["hints"])

    def test_settings_roundtrip(self):
        self.assertEqual(self.client.post("/api/settings", json={"model": "m-x", "dangerous": ["bash_"]}).json(), {"ok": True})
        s = self.client.get("/api/session").json()["session"]
        self.assertEqual((s["model"], s["dangerous"]), ("m-x", ["bash_"]))

    # ── history / files / commands ─────────────────────────────────

    def test_messages_rewind_and_files(self):
        store_msgs(self.proj, [
            {"role": "user", "content": "first"}, {"role": "assistant", "content": "a1"},
            {"role": "user", "content": "second"}, {"role": "assistant", "content": "a2"},
        ])
        msgs = self.client.get("/api/messages").json()["messages"]
        self.assertEqual([m["role"] for m in msgs], ["user", "assistant", "user", "assistant"])
        opts = self.client.get("/api/rewind").json()["options"]
        self.assertEqual([o["label"] for o in opts], ["first", "second"])
        r = self.client.post("/api/rewind", json={"index": 2}).json()
        self.assertEqual(r["rewound_text"], "second")
        self.assertEqual(len(r["messages"]), 2)
        self.assertEqual(len(load_msgs(self.proj)), 2)

    def test_files_file_upload_and_traversal(self):
        (Path(self.proj) / "hello.txt").write_text("hi")
        self.assertIn("hello.txt", self.client.get("/api/files").json()["files"])
        self.assertEqual(self.client.get("/api/file", params={"path": "hello.txt"}).content, b"hi")
        self.assertEqual(self.client.get("/api/file", params={"path": "../../etc/passwd"}).status_code, 404)
        self.assertEqual(self.client.get("/api/file", params={"path": "/etc/passwd"}).status_code, 404)
        up = self.client.post("/api/upload", files={"file": ("a.txt", b"data")}).json()["path"]
        up2 = self.client.post("/api/upload", files={"file": ("a.txt", b"data2")}).json()["path"]
        self.assertEqual((up, up2), ("uploads/a.txt", "uploads/a_1.txt"))
        self.assertEqual((Path(self.proj) / up2).read_bytes(), b"data2")
        # an upload is hostile-named: path components must not survive
        up3 = self.client.post("/api/upload", files={"file": ("../../evil.txt", b"x")}).json()["path"]
        self.assertTrue(up3.startswith("uploads/"))

    def test_commands(self):
        store_msgs(self.proj, [{"role": "user", "content": "x"}])
        self.assertTrue(self.client.post("/api/command", json={"name": "/clear"}).json()["cleared"])
        self.assertEqual(load_msgs(self.proj), [])
        self.assertIn("Salapa", self.client.post("/api/command", json={"name": "/author"}).json()["message"])
        self.assertIn("not available", self.client.post("/api/command", json={"name": "/nope"}).json()["message"])

    def test_tui_only_prompt_command_is_refused_without_a_model_call(self):
        ev = _events(self.chat("/graph build x", [{"text": "SHOULD NOT RUN"}]))
        self.assertTrue(any(e["type"] == "error" and "TUI-only" in e["errorText"] for e in ev))
        self.assertEqual(len(_Model.script), 1)

    def test_non_user_tail_streams_empty_turn(self):
        r = self.client.post("/api/chat", json={"messages": [{"role": "assistant", "parts": []}]})
        self.assertEqual([e["type"] for e in _events(r)], ["start", "finish"])

    # ── every tool through /api/chat ───────────────────────────────

    def test_all_default_tools_run_through_the_gui(self):
        p = Path(self.proj)
        (p / "seed.txt").write_text("alpha\nbeta\n")
        steps = [
            ("bash_", {"command": "echo bashed", "description": "test"}, "bashed"),
            ("write_", {"file_path": "w.txt", "content": "one\ntwo\n"}, None),
            ("read_", {"file_path": "w.txt"}, "two"),
            ("edit_", {"file_path": "w.txt", "old_string": "two", "new_string": "TWO"}, None),
            ("glob_", {"pattern": "*.txt"}, "w.txt"),
            ("grep_", {"pattern": "alpha"}, "seed.txt"),
            ("todo_tool_", {"todos": {"1": {"content": "do it", "status": "in_progress", "activeForm": "doing"}}}, None),
            ("memory_", {"action": "add", "key": "k1", "description": "a full descriptive sentence for the test memory", "content": "body", "scope": "project"}, "Added memory"),
            ("memory_", {"action": "list", "scope": "project"}, "k1"),
            ("search_history_", {"query": "run everything"}, None),
            ("list_skills", {}, None),
            ("list_mcps", {}, None),
            ("search_tools", {"action": "discover"}, "message_session_"),
            ("search_tools", {"action": "add", "names": "message_session_"}, None),
            ("use_tool_", {"name": "message_session_", "args": {"action": "list"}}, None),
            ("monitor_", {"action": "list"}, None),
            ("monitor_", {"action": "schedules"}, None),
            ("monitor_", {"action": "check", "targets": ["/definitely/not/a/subagent"]}, None),
        ]
        r = self.chat("run everything", [{"tool": n, "args": a} for n, a, _ in steps] + [{"text": "all done"}])
        ev = _events(r)
        self.assertEqual(r.status_code, 200)
        self.assertFalse([e for e in ev if e["type"] == "error"], ev)
        calls = [e for e in ev if e["type"] == "tool-input-available"]
        outs = {e["toolCallId"]: e["output"] for e in ev if e["type"] == "tool-output-available"}
        self.assertEqual([c["toolName"] for c in calls], ["message_session_" if n == "use_tool_" else n for n, _, _ in steps])  # dispatcher resolves to the inner tool
        for c, (name, _, needle) in zip(calls, steps):
            out = outs.get(c["toolCallId"])
            self.assertIsNotNone(out, f"{name}: no tool-output")
            text = out if isinstance(out, str) else json.dumps(out)
            self.assertFalse(text.startswith("Error"), f"{name}: {text[:200]}")
            if needle:
                self.assertIn(needle, text, name)
        self.assertEqual((p / "w.txt").read_text(), "one\nTWO\n")
        todos = [e for e in ev if e["type"] == "data-todos"]
        self.assertTrue(todos and todos[-1]["data"]["todos"])
        self.assertTrue(any(e["type"] == "text-delta" for e in ev))
        self.assertEqual(ev[-1]["type"], "finish")
        self.assertTrue(load_msgs(self.proj))  # persisted

    def test_background_process_shows_in_status_pill(self):
        from micro_cc.tools.bash_tool import kill_background_processes
        try:
            ev = _events(self.chat("bg", [{"tool": "bash_", "args": {"command": "sleep 60 &", "description": "test"}}, {"text": "ok"}]))
            self.assertFalse([e for e in ev if e["type"] == "error"], ev)
            parts = [e for e in ev if e["type"] == "data-bgprocs"]
            self.assertTrue(parts and any("sleep 60" in p["command"] for p in parts[-1]["data"]["processes"]), parts)
            procs = self.client.get("/api/background-status").json()["processes"]
            self.assertTrue(any("sleep 60" in p["command"] for p in procs), procs)
        finally:
            kill_background_processes()

    # ── gating: approve / deny / question ──────────────────────────

    def _gate(self, tools):
        self.client.post("/api/settings", json={"dangerous": tools})

    def test_approval_approve_resumes_the_parked_loop(self):
        self._gate(["bash_"])
        ev = _events(self.chat("go", [{"tool": "bash_", "args": {"command": "echo gated", "description": "test"}}, {"text": "finished"}]))
        self.assertEqual(ev[-1]["type"], "finish")
        self.assertTrue(any(e["type"] == "tool-approval-request" for e in ev))
        self.assertEqual(session.STATE["pending_kind"], "approval")
        self.assertFalse(any(e["type"] == "tool-output-available" for e in ev))
        ev2 = _events(self.client.post("/api/chat", json={"messages": [{"role": "assistant", "parts": []}]}))
        out = [e for e in ev2 if e["type"] == "tool-output-available"]
        self.assertTrue(out and "gated" in json.dumps(out[0]["output"]))
        self.assertIsNone(session.STATE["pending"])

    def test_approval_deny_skips_tool_and_model_replies(self):
        self._gate(["bash_"])
        marker = Path(self.proj) / "ran.txt"
        self.chat("go", [{"tool": "bash_", "args": {"command": f"touch {marker}", "description": "test"}}, {"text": "what instead?"}])
        self.assertEqual(self.client.post("/api/chat/deny").json(), {"ok": True})
        ev = _events(self.client.post("/api/chat", json={"messages": [{"role": "assistant", "parts": []}]}))
        out = [e for e in ev if e["type"] == "tool-output-available"]
        self.assertIn("declined", json.dumps(out[0]["output"]))
        self.assertTrue(any(e["type"] == "text-delta" for e in ev))  # the turn continued
        self.assertFalse(marker.exists())
        self.assertIsNone(session.STATE["pending"])
        self.assertFalse(self.client.post("/api/chat/deny").json()["ok"])

    def test_ask_user_question_roundtrip(self):
        q = [{"question": "Which?", "header": "Pick", "options": [{"label": "A", "description": "a"}, {"label": "B", "description": "b"}], "multiSelect": False}]
        ev = _events(self.chat("ask", [{"tool": "ask_user_question_tool_", "args": {"questions": q}}, {"text": "thanks"}]))
        dq = [e for e in ev if e["type"] == "data-question"]
        self.assertEqual(dq[0]["data"]["questions"][0]["header"], "Pick")
        self.assertEqual(session.STATE["pending_kind"], "question")
        self.assertTrue(self.client.post("/api/chat/answer", json={"answers": {"Pick": "B"}}).json()["ok"])
        ev2 = _events(self.client.post("/api/chat", json={"messages": [{"role": "assistant", "parts": []}]}))
        out = [e for e in ev2 if e["type"] == "tool-output-available"]
        self.assertIn("B", json.dumps(out[0]["output"]))

    def test_answer_without_question_is_rejected(self):
        self.assertFalse(self.client.post("/api/chat/answer", json={"answers": {}}).json()["ok"])

    # ── subagents / server-initiated turns ─────────────────────────

    def test_subagents_endpoint_reflects_tracker_and_hides_notified_done(self):
        from micro_cc.utils import subagent_tracker_ as tr
        from micro_cc.utils.msg_store_ import store_msgs as store
        sub = tempfile.mkdtemp(prefix="mcc-sub-")
        tr.add_tracked(self.proj, sub)  # a child registers before its run writes
        store(sub, [{"role": "user", "content": "go"},
                    {"role": "assistant", "content": "working\nSTATUS: DONE — wrote the report"}])
        row = self.client.get("/api/subagents").json()["subagents"][0]
        self.assertEqual(row["target"], sub)
        self.assertEqual((row["name"], row["status"], row["summary"]), (os.path.basename(sub), "DONE", "wrote the report"))
        # unchanged transcript -> poll_status returns None; last info must survive
        again = self.client.get("/api/subagents").json()["subagents"][0]
        self.assertEqual(again["name"], row["name"])
        self.assertEqual(again["status"], row["status"])
        tr.update_status(self.proj, sub, "FAILED")  # TUI poller's pid-death override beats the unchanged transcript
        self.assertEqual(self.client.get("/api/subagents").json()["subagents"][0]["status"], "FAILED")
        tr.mark_notified(self.proj, sub)
        self.assertEqual(self.client.get("/api/subagents").json()["subagents"], [])

    def test_incoming_is_offered_when_idle_and_consumed_by_the_chat_post(self):
        session.push_incoming("[subagent checkpoint] a")
        session.push_incoming("[subagent checkpoint] b")
        self.assertIsNone(self.client.get("/api/incoming").json()["content"])  # no claim -> not offered
        session.set_pending("approval", {"approved": None}, {"id": "x", "name": "bash_", "input": {}})
        self.assertIsNone(self.client.get("/api/incoming", params={"claim": True}).json()["content"])
        session.clear_pending()
        head = self.client.get("/api/incoming", params={"claim": True}).json()
        self.assertEqual(head["content"], "[subagent checkpoint] a")
        # peeking twice (a dropped send) does not consume it
        self.assertEqual(self.client.get("/api/incoming", params={"claim": True}).json()["id"], head["id"])
        _Model.script = [{"text": "noted"}]
        body = {**_user(head["content"]), "incoming_id": head["id"]}
        self.assertTrue(any(e["type"] == "text-delta" for e in _events(self.client.post("/api/chat", json=body))))
        # a second tab sending the same id gets an empty turn and no model call
        _Model.script = [{"text": "SHOULD NOT RUN"}]
        self.assertEqual([e["type"] for e in _events(self.client.post("/api/chat", json=body))], ["start", "finish"])
        self.assertEqual(len(_Model.script), 1)
        self.assertEqual(self.client.get("/api/incoming", params={"claim": True}).json()["content"], "[subagent checkpoint] b")

    def test_deny_waits_for_the_turn_lock(self):
        self._gate(["bash_"])
        self.chat("go", [{"tool": "bash_", "args": {"command": "echo x", "description": "test"}}, {"text": "x"}])
        self.assertEqual(self.client.post("/api/chat/deny").json(), {"ok": True})
        self.assertFalse(session.STATE["lock"].locked())

    def test_resume_after_gui_starts_one_turn_and_queues_the_rest(self):
        import asyncio
        from types import SimpleNamespace
        from micro_cc.start_live_tui_ import MicroTui
        session.push_incoming("w1")
        session.push_incoming("w2")
        calls = []

        async def inject(content):
            calls.append(("inject", content))
            fake._input_mode = "query_active"  # first inject starts a turn

        async def drain():
            calls.append(("drain",))

        async def reload():
            calls.append(("reload",))

        fake = SimpleNamespace(_input_mode="idle", _reload_messages_from_disk=reload,
                               _inject_incoming_turn=inject, _drain_next_queued=drain)
        asyncio.run(MicroTui._resume_after_gui(fake))
        self.assertEqual(calls, [("reload",), ("inject", "w1"), ("inject", "w2")])  # no drain over a live turn
        self.assertEqual(session.STATE["incoming"], [])

    def test_tui_routes_wakeups_to_gui_while_gui_owns_the_session(self):
        import asyncio
        from types import SimpleNamespace
        from micro_cc.start_live_tui_ import MicroTui
        fake = SimpleNamespace(_input_mode="gui")
        asyncio.run(MicroTui._inject_incoming_turn(fake, "[monitor: x] line"))
        self.assertEqual([i["content"] for i in session.STATE["incoming"]], ["[monitor: x] line"])

    def test_claimed_wakeup_runs_as_a_normal_turn(self):
        session.push_incoming("[subagent checkpoint]\nworker DONE")
        content = self.client.get("/api/incoming", params={"claim": True}).json()["content"]
        ev = _events(self.chat(content, [{"text": "noted"}]))
        self.assertTrue(any(e["type"] == "text-delta" for e in ev))
        self.assertIn("worker DONE", json.dumps(load_msgs(self.proj)))

    # ── one authenticated tab ──────────────────────────────────────

    def test_link_token_is_one_time_and_binds_the_first_tab(self):
        session.init(self.proj, msgs=[], model="fake", trim_budget=100_000, token="tok123")
        c = TestClient(server.app, follow_redirects=False)
        self.assertEqual(c.get("/api/session").status_code, 403)  # no cookie
        self.assertEqual(c.get("/", params={"t": "wrong"}).status_code, 403)
        r = c.get("/", params={"t": "tok123"})
        self.assertEqual((r.status_code, r.headers["location"]), (303, "/"))
        self.assertIn("HttpOnly", r.headers["set-cookie"])
        self.assertEqual(TestClient(server.app, follow_redirects=False).get("/", params={"t": "tok123"}).status_code, 403)  # replay
        self.assertEqual(c.get("/api/session", headers={"X-Tab": "A"}).status_code, 200)
        self.assertEqual(c.get("/api/ping", headers={"X-Tab": "A"}).status_code, 200)
        self.assertEqual(c.get("/api/ping", headers={"X-Tab": "B"}).status_code, 409)  # second tab, same browser
        self.assertEqual(c.get("/api/file", params={"path": "x"}).status_code, 404)  # <img> loads carry the cookie only
        self.assertEqual(c.get("/").status_code, 200)

    def test_concurrent_chat_posts_serialize_instead_of_corrupting_history(self):
        _Model.script = [{"text": "reply one"}, {"text": "reply two"}]
        out = []
        ts = [threading.Thread(target=lambda t=t: out.append(self.client.post("/api/chat", json=_user(t)).status_code)) for t in ("first", "second")]
        for t in ts:
            t.start()
        for t in ts:
            t.join(timeout=30)
        self.assertEqual(out, [200, 200])
        self.assertEqual([m["role"] for m in load_msgs(self.proj)], ["user", "assistant", "user", "assistant"])


    # ── durability: an interrupted turn is on disk ────────────────

    def _live(self):
        import socket
        import httpx
        from micro_cc.webui.launch import serve
        with socket.socket() as sk:
            sk.bind(("127.0.0.1", 0))
            port = sk.getsockname()[1]
        url, shutdown = serve(self.proj, model="fake", open_browser=False, host="127.0.0.1", port=port)
        client = httpx.Client(follow_redirects=True, timeout=30)
        client.get(url)  # token -> cookie
        return client, url.split("/?")[0], shutdown

    def _stream_some(self, client, base):
        import time
        _Model.script = [{"text": "word " * 120}]
        ctx = client.stream("POST", base + "/api/chat", json=_user("stop me"))
        r = ctx.__enter__()
        seen = ""
        for line in r.iter_lines():
            seen += line
            if seen.count("text-delta") >= 6:
                break
        return ctx, time

    def test_client_abort_mid_stream_persists_user_turn_and_partial_reply(self):
        client, base, shutdown = self._live()
        try:
            ctx, time = self._stream_some(client, base)
            ctx.__exit__(None, None, None)  # browser stop() aborts the fetch
            time.sleep(1.5)
            msgs = load_msgs(self.proj)
        finally:
            shutdown()
        self.assertEqual([m["role"] for m in msgs], ["user", "assistant"])
        self.assertIn("stop me", json.dumps(msgs[0]))
        self.assertTrue(msgs[1]["content"].startswith("word"))

    def test_gui_shutdown_mid_stream_persists_before_the_terminal_resumes(self):
        client, base, shutdown = self._live()
        ctx, time = self._stream_some(client, base)
        shutdown()  # esc in the TUI
        msgs = load_msgs(self.proj)
        ctx.__exit__(None, None, None)
        self.assertEqual([m["role"] for m in msgs], ["user", "assistant"])

    def test_stream_carries_subagent_status_as_a_data_part(self):
        from micro_cc.utils import subagent_tracker_ as tr
        sub = tempfile.mkdtemp(prefix="mcc-sub-")
        tr.add_tracked(self.proj, sub)  # a child registers before its run writes
        store_msgs(sub, [{"role": "user", "content": "go"}, {"role": "assistant", "content": "STATUS: NEEDS_INPUT — which db?"}])
        ev = _events(self.chat("hi", [{"text": "ok"}]))
        parts = [e for e in ev if e["type"] == "data-subagents"]
        self.assertEqual(len(parts), 1)  # unchanged snapshots are not re-sent
        row = parts[0]["data"]["subagents"][0]
        self.assertEqual((row["name"], row["status"]), (os.path.basename(sub), "NEEDS_INPUT"))

    def test_stop_endpoint_sets_flag(self):
        self.assertEqual(self.client.post("/api/chat/stop").json(), {"status": "stopping"})
        self.assertTrue(session.STATE["stop"].is_set())


if __name__ == "__main__":
    unittest.main()
