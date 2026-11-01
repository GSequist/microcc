"""Poll watches, persisted monitors, unattended OAuth and queued-wake merging.

Run directly:  python tests/test_monitor_poll.py
Temp HOME, shell probes over a temp file; no network.
"""

import asyncio
import json
import os
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

os.environ["HOME"] = tempfile.mkdtemp()
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "src"))

from micro_cc.tools import monitor_poll_ as mp, monitor_store_ as store, monitor_watch_ as mw  # noqa: E402
from micro_cc.tools import mcp_oauth  # noqa: E402
from micro_cc.tools.monitor_ import monitor_  # noqa: E402


def _write(path, text, mode="a"):
    with open(path, mode) as f:
        f.write(text)


class Items(unittest.TestCase):
    def test_lines(self):
        self.assertEqual(mp.extract_items("a\n\n b \n", None), [("a", "a"), ("b", "b")])

    def test_key_json_nested(self):
        out = json.dumps({"result": {"threads": [{"id": "1", "s": "hi"}, {"id": "2"}]}})
        self.assertEqual([k for k, _ in mp.extract_items(out, "id")], ["1", "2"])

    def test_key_jsonl(self):
        self.assertEqual([k for k, _ in mp.extract_items('{"id":1}\n{"id":2}\n', "id")], ["1", "2"])

    def test_key_missing_raises(self):
        with self.assertRaises(ValueError):
            mp.extract_items('[{"x":1}]', "id")


class Polls(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.pd = tempfile.mkdtemp()
        self.feed = os.path.join(self.pd, "feed.txt")
        _write(self.feed, "old1\nold2\n", "w")
        self.wakes = []
        mp.set_wake_callback(self.wakes.append)

    async def asyncTearDown(self):
        mp.kill_all()
        mp.set_wake_callback(None)

    async def _start(self, **kw):
        out = await monitor_("poll", project_dir=self.pd, spec="5m", description="feed",
                             command=f"cat {self.feed}", prompt="handle them", **kw)
        self.assertIn("Poll started", out, out)
        return out.split("Poll started: ")[1].split(" ")[0]

    async def test_refused_without_listener(self):
        mp.set_wake_callback(None)
        out = await monitor_("poll", project_dir=self.pd, spec="5m", description="x", command="echo a")
        self.assertIn("unavailable", out)

    async def test_validation(self):
        out = await monitor_("poll", project_dir=self.pd, spec="5m", description="x")
        self.assertIn("exactly one of command or mcp_tool", out)
        out = await monitor_("poll", project_dir=self.pd, spec="soon", description="x", command="echo")
        self.assertIn("invalid poll interval", out)

    async def test_baseline_then_only_new_items_wake(self):
        pid = await self._start()
        self.assertEqual(await mp.tick(pid), 300)
        self.assertEqual(self.wakes, [])  # nothing new: no tokens spent
        _write(self.feed, "new1\nnew1\n")
        await mp.tick(pid)
        self.assertEqual(len(self.wakes), 1)
        self.assertIn("[monitor: feed]", self.wakes[0])
        self.assertIn("handle them", self.wakes[0])
        self.assertIn("1 new item:", self.wakes[0])
        self.assertNotIn("old1", self.wakes[0])
        await mp.tick(pid)
        self.assertEqual(len(self.wakes), 1)  # already seen

    async def test_failures_back_off_then_stop(self):
        pid = await self._start()
        os.remove(self.feed)
        delays = [await mp.tick(pid) for _ in range(mp.MAX_FAILS)]
        self.assertEqual(delays[:2], [600, 1200])
        self.assertEqual(delays[-1], 0)
        self.assertNotIn(pid, mp._polls)
        self.assertIn("stopped", self.wakes[-1])
        self.assertEqual(store.load(self.pd)["polls"], {})

    async def test_expiry_stops(self):
        pid = await self._start(max_age_days=1)
        self.assertEqual(await mp.tick(pid, now=time.time() + 2 * 86400), 0)
        self.assertIn("time limit", self.wakes[-1])

    async def test_stop_list_check(self):
        pid = await self._start()
        self.assertIn(pid, await monitor_("list", project_dir=self.pd))
        self.assertIn(f"poll {pid}", await monitor_("check", project_dir=self.pd, targets=[pid]))
        self.assertEqual(await monitor_("stop", project_dir=self.pd, watch_id=pid), f"Poll {pid} stopped.")
        self.assertEqual(store.load(self.pd)["polls"], {})

    async def test_all_digit_id_is_not_a_pid(self):
        with patch.object(mp.uuid, "uuid4", lambda: type("U", (), {"hex": "06005364ffff"})()):
            pid = await self._start()
        self.assertEqual(pid, "06005364")
        self.assertIn("poll 06005364", await monitor_("check", project_dir=self.pd, targets=[pid]))

    async def test_rearm_rules(self):
        session = await self._start()
        durable = await self._start(durable=True)
        mp.kill_all()  # self-reload: same pid, both come back
        self.assertEqual(mp.rearm(self.pd), 2)
        mp.kill_all()
        data = store.load(self.pd)
        for rec in data["polls"].values():
            rec["pid"] = -1  # a fresh process: only the durable one survives
        store._save(self.pd, data)
        self.assertEqual(mp.rearm(self.pd), 1)
        self.assertIn(durable, mp._polls)
        self.assertNotIn(session, store.load(self.pd)["polls"])
        _write(self.feed, "new\n")
        await mp.tick(durable)
        self.assertIn("new", self.wakes[-1])  # seen-set survived the restart


class Watches(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.pd = tempfile.mkdtemp()
        mw.set_wake_callback(lambda c: None)

    async def asyncTearDown(self):
        for w in mw.list_watches():
            await mw.stop_watch(w["watch_id"])
        mw.set_wake_callback(None)

    async def test_persistent_watch_rearms_after_kill_all(self):
        out = await mw.start_watch("sleep 30", "w", project_dir=self.pd, persistent=True)
        wid = out.split("Watch started: ")[1].split(" ")[0]
        mw.kill_all()
        await asyncio.sleep(0.1)
        self.assertIn(wid, store.load(self.pd)["watches"])
        self.assertEqual(await mw.rearm(self.pd), 1)
        self.assertEqual([w["watch_id"] for w in mw.list_watches()], [wid])
        await mw.stop_watch(wid)
        self.assertEqual(store.load(self.pd)["watches"], {})

    async def test_timed_watch_not_persisted(self):
        await mw.start_watch("sleep 30", "w", project_dir=self.pd, timeout_ms=60_000)
        self.assertEqual(store.load(self.pd)["watches"], {})


class UnattendedOAuth(unittest.IsolatedAsyncioTestCase):
    async def test_dead_refresh_raises_instead_of_login(self):
        url = "https://mcp.example/mcp"
        mcp_oauth._save(url, access_token="t", expires_at=1, refresh_token=None)

        async def no_login(*a, **k):
            raise AssertionError("login must not run unattended")
        with patch.object(mcp_oauth, "login", no_login):
            tok = mcp_oauth.UNATTENDED.set(True)
            try:
                with self.assertRaises(mcp_oauth.AuthExpired):
                    await mcp_oauth.oauth_headers(url)
            finally:
                mcp_oauth.UNATTENDED.reset(tok)

    async def test_poll_stops_on_auth_expired(self):
        pd = tempfile.mkdtemp()
        wakes = []
        mp.set_wake_callback(wakes.append)
        rec = {"id": "auth1", "description": "mail", "every": 300, "project_dir": pd, "seen": [],
               "created_at": time.time(), "mcp_server": "x", "mcp_remote": "y"}
        mp._polls["auth1"] = rec

        async def expired(*a, **k):
            raise mcp_oauth.AuthExpired("OAuth for x expired")
        with patch.object(mp, "_run_mcp", expired):
            self.assertEqual(await mp.tick("auth1"), 0)
        self.assertIn("expired", wakes[-1])
        mp.set_wake_callback(None)


class QueueMerge(unittest.IsolatedAsyncioTestCase):
    async def test_monitor_events_merge_while_busy(self):
        from collections import deque
        from micro_cc.start_live_tui_ import MicroTui

        class Rows:
            def __init__(self): self.rows = []
            def remove(self, r): self.rows.remove(r)

        class Fake:
            _input_mode = "query_active"
            _queue = deque()
            _msg_rows = []
            message_list = Rows()

            async def _mount_row(self, msg):
                self.message_list.rows.append(msg)
                self._msg_rows.append(msg)
                return msg
        app = Fake()
        await MicroTui._inject_incoming_turn(app, "[monitor: a]\nx")
        await MicroTui._inject_incoming_turn(app, "[monitor: a]\ny")
        await MicroTui._inject_incoming_turn(app, "typed by user")
        await MicroTui._inject_incoming_turn(app, "[monitor: a]\nz")
        self.assertEqual([q[0] for q in app._queue], ["[monitor: a]\nx\n\n[monitor: a]\ny", "typed by user", "[monitor: a]\nz"])
        self.assertEqual(len(app.message_list.rows), 3)


if __name__ == "__main__":
    unittest.main(verbosity=1)
