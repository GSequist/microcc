"""Subagent lifecycle + bounded-tool scenarios.

Real: file tools against temp trees, the tracker's flock across real OS
processes, the detached spawn (real `python -m micro_cc.start_headless_`
parent/child processes), SIGTERM handling, bash_ process-group cleanup, and
msg_store persistence. Faked: the model endpoint is a local HTTP server
speaking the OpenAI chat-completions SSE shape (LiteLLM route); HOME is a
temp dir and every provider routing key is pinned empty so no real
credentials (e.g. a repo .env picked up by load_dotenv) can be used and no
network call leaves the machine.
"""

import asyncio
import glob
import http.server
import json
import os
from pathlib import Path
import signal
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
import unittest

SRC = str(Path(__file__).resolve().parents[1] / "src")
sys.path.insert(0, SRC)

_TMP_HOME = tempfile.mkdtemp(prefix="mcc-home-")
os.environ["HOME"] = _TMP_HOME  # before any micro_cc import resolves ~/.micro-cc

_ROUTING_KEYS = [
    "OLLAMA_BASE_URL", "ANTHROPIC_API_KEY", "ANTHROPIC_OAUTH_TOKEN", "FOUNDRY_BASE_URL",
    "FOUNDRY_API_KEY", "LITELLM_BASE_URL", "LITELLM_API_KEY", "OPENROUTER_API_KEY",
    "OPENROUTER_BASE_URL", "OPENAI_BASE_URL", "OPENAI_API_KEY", "MICRO_CC_POSTGRES_URL",
]
for _k in _ROUTING_KEYS:
    os.environ[_k] = ""  # load_dotenv never overrides an existing var

from micro_cc import execute_tool  # noqa: E402
from micro_cc.tools import file_tools_ as ft  # noqa: E402
from micro_cc.utils import subagent_tracker_ as tracker  # noqa: E402
from micro_cc.utils.msg_store_ import load_msgs, store_msgs, _get_storage_dir  # noqa: E402


def _child_env(**extra) -> dict:
    env = {**os.environ, "HOME": _TMP_HOME, "PYTHONPATH": SRC}
    env.update(extra)
    return env


class ToolBoundsTests(unittest.TestCase):
    def test_sync_tool_runs_off_loop_and_times_out_with_note(self):
        ticks = []

        def slow_tool():
            time.sleep(1.0)
            return "done"

        def hung_tool():
            time.sleep(30)

        class TB:
            def __init__(self, name):
                self.name, self.input = name, {}

        async def ticker():
            for _ in range(8):
                ticks.append(time.monotonic())
                await asyncio.sleep(0.1)

        async def run():
            t = asyncio.create_task(ticker())
            res = await execute_tool.execute_tool_call(TB("slow_tool"), {"slow_tool": slow_tool}, "/tmp", "m")
            await t
            old = execute_tool.SYNC_TOOL_TIMEOUT
            execute_tool.SYNC_TOOL_TIMEOUT = 0.3
            try:
                t0 = time.monotonic()
                hung = await execute_tool.execute_tool_call(TB("hung_tool"), {"hung_tool": hung_tool}, "/tmp", "m")
                elapsed = time.monotonic() - t0
            finally:
                execute_tool.SYNC_TOOL_TIMEOUT = old
            return res, hung, elapsed

        res, hung, elapsed = asyncio.run(run())
        self.assertEqual(res, "done")
        # the loop kept ticking while the sync tool slept (was: frozen for 1s)
        self.assertGreaterEqual(len([t for t in ticks if t - ticks[0] < 0.95]), 6)
        self.assertIn("did not return within", hung)
        self.assertLess(elapsed, 2)

    def test_abandoned_hung_thread_does_not_block_process_exit(self):
        code = (
            "import asyncio,time,sys\n"
            "from micro_cc import execute_tool as e\n"
            "e.SYNC_TOOL_TIMEOUT=0.2\n"
            "class TB: name='h'; input={}\n"
            "print(asyncio.run(e.execute_tool_call(TB(), {'h': lambda: time.sleep(600)}, '/tmp', 'm'))[:30])\n"
        )
        t0 = time.monotonic()
        out = subprocess.run([sys.executable, "-c", code], env=_child_env(), capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertLess(time.monotonic() - t0, 15)

    def _tree(self) -> str:
        d = tempfile.mkdtemp(prefix="mcc-tree-")
        for rel in ["a.py", "b.txt", ".hidden.py", "sub/c.py", "sub/deep/d.py", ".git/e.py", "sub/.h/f.py"]:
            p = os.path.join(d, rel)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            Path(p).write_text("needle here\n" if rel.endswith(".py") else "nothing\n")
        os.symlink(d, os.path.join(d, "sub", "loop"))  # symlink cycle
        os.mkfifo(os.path.join(d, "pipe"))
        return d

    def test_glob_walker_matches_glob_glob_and_survives_symlink_cycle(self):
        d = self._tree()
        for pat in ["*.py", "**/*.py", "sub/*.py", "**", "**/", "sub/**/*.py", ".*", "**/.h/*"]:
            full = os.path.join(d, pat)
            ours = list(ft._iter_glob(full, ft._WalkStats(time.monotonic() + 30)))
            if "**" in pat:
                # glob.glob recurses through the symlink cycle until ELOOP; the
                # walker enters each real dir once. Compare on the loop-free part.
                expected = {p for p in glob.glob(full, recursive=True) if "/loop/" not in p + "/"}
                self.assertEqual({p for p in ours if "/loop/" not in p + "/"}, expected, pat)
            else:
                self.assertEqual(set(ours), set(glob.glob(full, recursive=True)), pat)
            self.assertEqual(len(ours), len(set(ours)), f"duplicates for {pat}")

    def test_glob_and_grep_say_when_they_stop_early(self):
        d = self._tree()
        old = ft.GLOB_SCAN_CAP
        ft.GLOB_SCAN_CAP = 2
        try:
            out = ft.glob_("**/*.py", project_dir=d)
        finally:
            ft.GLOB_SCAN_CAP = old
        self.assertIn("stopped early", out)
        self.assertIn("find ", out)

        old = ft.GREP_DEADLINE_S
        ft.GREP_DEADLINE_S = 0
        try:
            out = ft.grep_("needle", project_dir=d)
        finally:
            ft.GREP_DEADLINE_S = old
        self.assertIn("partial results", out)
        self.assertIn("rg -n", out)

        out = ft.grep_("needle", project_dir=d)
        self.assertIn("FIFO/socket/device", out)  # the fifo is reported, not silently passed over
        self.assertIn("sub/deep/d.py", out)

    def test_read_refuses_fifo_without_blocking_and_streams_window(self):
        d = self._tree()
        t0 = time.monotonic()
        out = ft.read_("pipe", project_dir=d)
        self.assertLess(time.monotonic() - t0, 1)
        self.assertIn("Not a regular file", out)
        big = os.path.join(d, "big.txt")
        Path(big).write_text("".join(f"line{i}\n" for i in range(5000)))
        out = ft.read_(big, project_dir=d, offset=10, limit=2)
        self.assertIn("    11\tline10", out)
        self.assertIn("[... 4988 more lines]", out)


class RepairTests(unittest.TestCase):
    def test_mid_history_repair_is_stable_across_reloads(self):
        pd = tempfile.mkdtemp(prefix="mcc-proj-")
        store_msgs(pd, [
            {"role": "user", "content": "go"},
            {"role": "assistant", "ts": "2026-10-01T10:00:00", "content": [{"type": "tool_use", "id": "t1", "name": "read_", "input": {}}]},
            {"role": "user", "content": "continue"},
        ])
        for _ in range(3):
            msgs = load_msgs(pd)
            store_msgs(pd, msgs)
        lines = (_get_storage_dir(pd) / "messages.jsonl").read_text().splitlines()
        self.assertEqual(len(lines), 3)  # was: +1 duplicated tail per reload
        merged = load_msgs(pd)[2]
        self.assertEqual(merged["content"][0]["type"], "tool_result")
        self.assertEqual(merged["content"][-1], {"type": "text", "text": "continue"})


class CapTests(unittest.TestCase):
    def test_cap_counts_only_live_pid_entries(self):
        boss = tempfile.mkdtemp(prefix="mcc-boss-")
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait()
        path = _get_storage_dir(boss) / "tracked_subagents.json"
        path.write_text(json.dumps({"subagents": {
            "/x/live": {"status": "RUNNING", "notified": False, "pid": os.getpid()},
            "/x/dead": {"status": "RUNNING", "notified": False, "pid": dead.pid},
            "/x/placeholder": {"status": "RUNNING", "notified": False},
            "/x/done": {"status": "DONE", "notified": True, "pid": os.getpid()},
        }}))
        tracker.add_tracked(boss, "/x/new1", max_running=2)  # 1 live other -> ok
        with self.assertRaises(tracker.CapReached) as cm:
            tracker.add_tracked(boss, "/x/new2", max_running=2)
        self.assertIn("limit is 2", str(cm.exception))

    def test_cap_holds_under_simultaneous_registration_by_real_processes(self):
        boss = tempfile.mkdtemp(prefix="mcc-boss-")
        release = os.path.join(boss, "release")
        code = (
            "import os,sys,time\n"
            "from micro_cc.utils import subagent_tracker_ as t\n"
            "try:\n"
            f"    t.add_tracked({boss!r}, sys.argv[1], max_running=2); print('OK', flush=True)\n"
            "except t.CapReached: print('CAP', flush=True)\n"
            f"while not os.path.exists({release!r}): time.sleep(0.05)\n"
        )
        procs = [
            subprocess.Popen([sys.executable, "-c", code, f"/x/s{i}"], env=_child_env(), stdout=subprocess.PIPE, text=True)
            for i in range(6)
        ]
        try:
            results = [p.stdout.readline().strip() for p in procs]
        finally:
            Path(release).write_text("")
            for p in procs:
                p.wait(timeout=10)
                p.stdout.close()
        self.assertEqual(results.count("OK"), 2, results)
        self.assertEqual(results.count("CAP"), 4, results)


class _FakeChat(http.server.BaseHTTPRequestHandler):
    """One tool call per request: a bash_ that backgrounds a sleep, records
    both pids, then blocks — so a SIGTERM lands mid-tool with a live group."""
    pidfile = ""

    def log_message(self, *a):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        cmd = f"sleep 300 & echo $! > {self.pidfile}; sleep 300"
        chunks = [
            {"choices": [{"index": 0, "delta": {"role": "assistant", "tool_calls": [{
                "index": 0, "id": "call_1", "type": "function",
                "function": {"name": "bash_", "arguments": json.dumps({"command": cmd})},
            }]}, "finish_reason": None}]},
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
        ]
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for c in chunks:
            c.update({"id": "x", "object": "chat.completion.chunk", "created": 0, "model": "fake"})
            self.wfile.write(f"data: {json.dumps(c)}\n\n".encode())
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # a zombie still answers kill(0); ask ps for its state
    st = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    return bool(st) and not st.startswith("Z")


class DetachedSpawnTests(unittest.TestCase):
    def setUp(self):
        self.boss = tempfile.mkdtemp(prefix="mcc-boss-")
        self.work = tempfile.mkdtemp(prefix="mcc-work-")
        _FakeChat.pidfile = os.path.join(self.work, "bg.pid")
        self.httpd = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _FakeChat)
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.env = _child_env(
            MICROCC_CALLER_PROJECT_DIR=self.boss,
            LITELLM_BASE_URL=f"http://127.0.0.1:{self.httpd.server_address[1]}",
            LITELLM_API_KEY="test",
        )

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def _spawn(self, name: str):
        return subprocess.run(
            [sys.executable, "-m", "micro_cc.start_headless_", os.path.join(self.work, name),
             "do it", "--model", "fake-model", "--output-format", "json"],
            env=self.env, capture_output=True, text=True, timeout=60,
        )

    def test_started_then_sigterm_stops_gracefully(self):
        t0 = time.monotonic()
        out = self._spawn("agent1")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        verdict = json.loads(out.stdout.strip().splitlines()[-1])
        self.assertEqual(verdict["status"], "STARTED")
        self.assertLess(time.monotonic() - t0, 30)  # returned while the child keeps running
        pid = verdict["pid"]
        self.assertTrue(_pid_alive(pid))
        entry = tracker.read_tracked(self.boss)["subagents"][os.path.join(self.work, "agent1")]
        self.assertEqual(entry["pid"], pid)  # registered by the long-lived child itself

        deadline = time.monotonic() + 30
        while not (os.path.exists(_FakeChat.pidfile) and Path(_FakeChat.pidfile).read_text().strip()):
            self.assertLess(time.monotonic(), deadline, "tool never started")
            time.sleep(0.1)
        bg_pid = int(Path(_FakeChat.pidfile).read_text())

        # what the boss sees mid-tool (the transcript alone would say "waiting on the model")
        from micro_cc.tools.monitor_ import get_subagent_status, format_status_line
        info = get_subagent_status(os.path.join(self.work, "agent1"), caller_project_dir=self.boss)
        self.assertIn(f"pid {pid} alive", info["activity"])
        self.assertIn("running bash_ for", info["activity"])
        self.assertIn("activity:", format_status_line(info))

        os.kill(pid, signal.SIGTERM)
        deadline = time.monotonic() + 20
        while _pid_alive(pid):
            self.assertLess(time.monotonic(), deadline, "subagent did not exit after SIGTERM")
            time.sleep(0.1)
        time.sleep(0.3)
        self.assertFalse(_pid_alive(bg_pid), "subagent's shell child survived the kill")

        msgs = [json.loads(l) for l in (_get_storage_dir(os.path.join(self.work, "agent1")) / "messages.jsonl").read_text().splitlines()]
        self.assertEqual(msgs[-1]["role"], "assistant")
        self.assertIn("STATUS: FAILED — stopped by SIGTERM while running bash_", msgs[-1]["content"])
        # valid transcript: every tool_use answered, so a relaunch loads clean
        for i, m in enumerate(msgs):
            if m["role"] == "assistant" and isinstance(m["content"], list):
                ids = {b["id"] for b in m["content"] if b.get("type") == "tool_use"}
                nxt = msgs[i + 1]["content"] if i + 1 < len(msgs) else []
                got = {b.get("tool_use_id") for b in nxt if isinstance(b, dict)} if isinstance(nxt, list) else set()
                self.assertLessEqual(ids, got)

    def test_refused_synchronously_when_cap_reached(self):
        from micro_cc.utils import settings_store_
        settings_store_.edit_setting("max_subagents", 1)
        holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        try:
            tracker.add_tracked(self.boss, "/x/busy")
            data = tracker.read_tracked(self.boss)
            data["subagents"]["/x/busy"]["pid"] = holder.pid
            (_get_storage_dir(self.boss) / "tracked_subagents.json").write_text(json.dumps(data))
            out = self._spawn("agent2")
            self.assertEqual(out.returncode, 2, out.stdout + out.stderr)
            verdict = json.loads(out.stdout.strip().splitlines()[-1])
            self.assertEqual(verdict["status"], "REFUSED")
            self.assertIn("limit is 1", verdict["summary"])
            self.assertNotIn(os.path.join(self.work, "agent2"), tracker.read_tracked(self.boss)["subagents"])
        finally:
            holder.kill()
            holder.wait()
            settings_store_.edit_setting("max_subagents", settings_store_.DEFAULT_MAX_SUBAGENTS)


if __name__ == "__main__":
    result = unittest.main(exit=False, verbosity=1).result
    if result.wasSuccessful():
        print("OK: subagent lifecycle / bounded-tool properties held")
    sys.exit(0 if result.wasSuccessful() else 1)
