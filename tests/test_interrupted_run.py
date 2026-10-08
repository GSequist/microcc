"""Recovery text, cut-short helper, acked inbox, atomic summary and a killed headless run resuming.
Real: msg_normalize_, msg_store_ on a temp HOME, inbox_store_, run_headless with a
scripted model. Mocked: model_call, checkpoint/memory/token-stats storage. No network.
PostgresBackendTests run only when MICRO_CC_TEST_POSTGRES_URL is set (see docker-compose.yml).
"""

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

_TMP_HOME = tempfile.mkdtemp(prefix="mcc-home-")
os.environ["HOME"] = _TMP_HOME
os.environ["MICRO_CC_POSTGRES_URL"] = ""

from micro_cc import start_headless_ as headless  # noqa: E402
from micro_cc.utils import inbox_store_ as inbox, msg_store_  # noqa: E402
from micro_cc.utils.msg_normalize_ import REPAIR_RESULT_TEXT, cut_short_calls, repair_dangling_tool_use  # noqa: E402
from micro_cc.utils.msg_store_ import _get_storage_dir, load_checkpoint, load_msgs, store_checkpoint, store_msgs  # noqa: E402
from micro_cc.postgres_store import pg_store_  # noqa: E402
from micro_cc.utils.helpers import project_hash  # noqa: E402
from test_use_tool_dispatcher import LoopCase, Scripted, final, turn, use  # noqa: E402

PG_URL = os.environ.get("MICRO_CC_TEST_POSTGRES_URL", "")


def call(id, tool, **args):
    return {"role": "assistant", "content": [{"type": "tool_use", "id": id, "name": tool, "input": args}]}


def result_text(msgs, idx=-1):
    return msgs[idx]["content"][0]["content"]


class RepairTextTests(unittest.TestCase):
    def test_unsafe_call_is_named_and_told_to_check_state(self):
        out = repair_dangling_tool_use([{"role": "user", "content": "go"}, call("t1", "bash_", command="deploy.sh --apply")])
        text = result_text(out)
        self.assertTrue(text.startswith(REPAIR_RESULT_TEXT))
        self.assertIn("bash_(", text)
        self.assertIn("deploy.sh --apply", text)
        self.assertIn("may have partly run", text)

    def test_read_only_call_is_told_to_call_again(self):
        out = repair_dangling_tool_use([{"role": "user", "content": "go"}, call("t1", "read_", path="a.py")])
        self.assertIn("call it again", result_text(out))

    def test_read_only_depends_on_action(self):
        get = repair_dangling_tool_use([call("t1", "memory_", action="get", key="k")])
        add = repair_dangling_tool_use([call("t2", "memory_", action="add", key="k")])
        self.assertIn("call it again", result_text(get))
        self.assertIn("may have partly run", result_text(add))

    def test_dispatcher_is_unwrapped_to_the_inner_call(self):
        out = repair_dangling_tool_use([call("t1", "use_tool_", name="browser", args={"code": "1"})])
        text = result_text(out)
        self.assertIn("browser(", text)
        self.assertNotIn("use_tool_(", text)
        self.assertIn("may have partly run", text)

    def test_long_args_are_truncated(self):
        out = repair_dangling_tool_use([call("t1", "write_", content="x" * 5000)])
        self.assertLess(len(result_text(out)), len(REPAIR_RESULT_TEXT) + 400)

    def test_corrupt_call_blocks_still_get_an_answer(self):
        bad_args = {"role": "assistant", "content": [{"type": "tool_use", "id": "a", "name": "memory_", "input": "oops"}]}
        no_name = {"role": "assistant", "content": [{"type": "tool_use", "id": "b", "input": {}}]}
        self.assertIn("may have partly run", result_text(repair_dangling_tool_use([bad_args])))
        self.assertEqual(result_text(repair_dangling_tool_use([no_name])), REPAIR_RESULT_TEXT)

    def test_several_calls_each_get_their_own_text(self):
        msg = {"role": "assistant", "content": [
            {"type": "tool_use", "id": "a", "name": "read_", "input": {}},
            {"type": "tool_use", "id": "b", "name": "bash_", "input": {"command": "ls"}},
        ]}
        blocks = repair_dangling_tool_use([msg])[-1]["content"]
        self.assertEqual([b["tool_use_id"] for b in blocks], ["a", "b"])
        self.assertIn("call it again", blocks[0]["content"])
        self.assertIn("may have partly run", blocks[1]["content"])

    def test_mid_history_dangle_keeps_positions_and_merges(self):
        msgs = [{"role": "user", "content": "go"}, call("t1", "bash_", command="ls"), {"role": "user", "content": "continue"}]
        out = repair_dangling_tool_use(msgs)
        self.assertEqual(len(out), 3)
        self.assertEqual(out[2]["content"][0]["type"], "tool_result")
        self.assertEqual(out[2]["content"][-1], {"type": "text", "text": "continue"})

    def test_answered_calls_are_untouched(self):
        msgs = [call("t1", "bash_", command="ls"),
                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]}]
        self.assertEqual(repair_dangling_tool_use(msgs), msgs)


class CutShortTests(unittest.TestCase):
    def test_tail_repair_names_the_cut_calls(self):
        out = repair_dangling_tool_use([{"role": "user", "content": "go"}, call("t1", "use_tool_", name="browser", args={})])
        self.assertEqual(cut_short_calls(out), ["browser"])

    def test_stops_once_the_user_continues(self):
        out = repair_dangling_tool_use([{"role": "user", "content": "go"}, call("t1", "bash_", command="ls")])
        self.assertEqual(cut_short_calls(out + [{"role": "user", "content": "continue"}]), [])

    def test_real_results_and_empty_history_do_not_fire(self):
        done = [call("t1", "bash_", command="ls"),
                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]}]
        self.assertEqual(cut_short_calls(done), [])
        self.assertEqual(cut_short_calls([]), [])


class SummaryTests(unittest.TestCase):
    def test_failed_write_leaves_the_old_summary_whole(self):
        pd = tempfile.mkdtemp(prefix="mcc-proj-")
        store_checkpoint(pd, "first", 3, 10)
        with patch.object(os, "replace", side_effect=OSError("disk")):
            with self.assertRaises(OSError):
                store_checkpoint(pd, "second", 4, 20)
        self.assertEqual(load_checkpoint(pd), {"content": "first", "as_of_index": 3, "folded_tokens": 10})
        self.assertEqual([p.name for p in _get_storage_dir(pd).glob("*.tmp")], [])


class InboxTests(unittest.TestCase):
    def setUp(self):
        self.pd = tempfile.mkdtemp(prefix="mcc-proj-")

    def incoming(self, msgs):
        return [m for m in msgs if isinstance(m["content"], str) and m["content"].startswith("[incoming from")]

    def test_crash_between_flush_and_ack_neither_loses_nor_duplicates(self):
        inbox.write_mail(self.pd, "/boss", "hello")
        msgs = load_msgs(self.pd)
        with patch.object(headless, "ack_mail", side_effect=OSError("died")):
            with self.assertRaises(OSError):
                headless._fold_mail(msgs, self.pd)
        self.assertEqual(inbox.peek_pending_count(self.pd), 1)
        restarted = load_msgs(self.pd)
        self.assertEqual(len(self.incoming(restarted)), 1)
        headless._fold_mail(restarted, self.pd)
        self.assertEqual(len(self.incoming(restarted)), 1)
        self.assertEqual(inbox.peek_pending_count(self.pd), 0)

    def test_failed_flush_keeps_the_mail_pending(self):
        inbox.write_mail(self.pd, "/boss", "hello")
        with patch.object(headless, "store_msgs", side_effect=OSError("disk")):
            with self.assertRaises(OSError):
                headless._fold_mail([], self.pd)
        self.assertEqual(inbox.peek_pending_count(self.pd), 1)
        msgs = load_msgs(self.pd)
        headless._fold_mail(msgs, self.pd)
        self.assertEqual(len(self.incoming(msgs)), 1)

    def test_mail_arriving_after_the_peek_survives_the_ack(self):
        inbox.write_mail(self.pd, "/a", "one")
        seen = inbox.peek_mail(self.pd)
        inbox.write_mail(self.pd, "/b", "two")
        inbox.ack_mail(self.pd, [m["id"] for m in seen])
        left = inbox.peek_mail(self.pd)
        self.assertEqual([m["text"] for m in left], ["two"])

    def test_mail_written_before_ids_existed_gets_one(self):
        path = _get_storage_dir(self.pd) / "inbox.json"
        path.write_text(json.dumps([{"from": "/old", "text": "legacy", "timestamp": "t"}]))
        mail = inbox.peek_mail(self.pd)
        self.assertTrue(mail[0]["id"])
        inbox.ack_mail(self.pd, [mail[0]["id"]])
        self.assertEqual(inbox.peek_pending_count(self.pd), 0)


class MirrorUrlTests(unittest.TestCase):
    def test_mirror_url_wins_for_pg_store_and_is_hidden_from_the_model_shell(self):
        from micro_cc.tools.bash_tool import _bash_env
        env = {"MICRO_CC_MIRROR_POSTGRES_URL": "postgresql://mirror/db", "MICRO_CC_POSTGRES_URL": "postgresql://main/db"}
        seen = []
        fake = type("P", (), {"connect": staticmethod(lambda url, **kw: seen.append(url) or (_ for _ in ()).throw(RuntimeError("stop")))})
        with patch.dict(os.environ, env), patch.dict(sys.modules, {"psycopg": fake}):
            with self.assertRaises(RuntimeError):
                pg_store_._connect()
            hidden = _bash_env()
        self.assertEqual(seen, ["postgresql://mirror/db"])
        self.assertNotIn("MICRO_CC_MIRROR_POSTGRES_URL", hidden)
        self.assertNotIn("MICRO_CC_POSTGRES_URL", hidden)

    def test_falls_back_to_main_url_and_ignores_crm_name(self):
        env = {"CRM_POSTGRES_URL": "postgresql://crm/db", "MICRO_CC_POSTGRES_URL": "postgresql://main/db"}
        seen = []
        fake = type("P", (), {"connect": staticmethod(lambda url, **kw: seen.append(url) or (_ for _ in ()).throw(RuntimeError("stop")))})
        with patch.dict(os.environ, env), patch.dict(sys.modules, {"psycopg": fake}):
            os.environ.pop("MICRO_CC_MIRROR_POSTGRES_URL", None)
            with self.assertRaises(RuntimeError):
                pg_store_._connect()
        self.assertEqual(seen, ["postgresql://main/db"])


class HeadlessResumeTests(LoopCase):
    async def test_killed_run_resumes_with_tool_aware_text_and_no_duplicate_rows(self):
        pd = self.tmp.name
        store_msgs(pd, [{"role": "user", "content": "task 7"}, call("t1", "bash_", command="deploy.sh --apply")])
        sunk = []
        msg_store_.set_sink(lambda _p, m: sunk.append(len(m)))
        self.addCleanup(msg_store_.set_sink, None)
        model = Scripted(final("handled"))
        with patch("micro_cc.claude_loop_.model_call", model):
            await headless.run_headless(pd, "continue task 7")
        sent = [b for m in model.inputs[0] if isinstance(m.get("content"), list) for b in m["content"] if b.get("type") == "tool_result"]
        self.assertIn("Call: bash_(", sent[0]["content"])
        self.assertIn("may have partly run", sent[0]["content"])
        lines = (_get_storage_dir(pd) / "messages.jsonl").read_text().splitlines()
        roles = [json.loads(line)["role"] for line in lines]
        self.assertEqual(roles, ["user", "assistant", "user", "user", "assistant"])
        self.assertEqual(sunk[-1], 5)


@unittest.skipUnless(PG_URL, "set MICRO_CC_TEST_POSTGRES_URL to run against Postgres")
class PostgresBackendTests(LoopCase):
    def setUp(self):
        super().setUp()
        env = patch.dict(os.environ, {"MICRO_CC_POSTGRES_URL": PG_URL})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("MICRO_CC_MIRROR_POSTGRES_URL", None)
        self.pd = self.tmp.name
        self.addCleanup(self.wipe)

    def wipe(self):
        with pg_store_._connect() as conn:
            for table in ("micro_cc_messages", "micro_cc_sessions"):
                conn.execute(f"DELETE FROM {table} WHERE project_hash = %s", (project_hash(self.pd),))

    def rows(self):
        with pg_store_._connect() as conn:
            return conn.execute("SELECT COUNT(*) FROM micro_cc_messages WHERE project_hash = %s", (project_hash(self.pd),)).fetchone()[0]

    def test_dangling_tail_is_repaired_with_tool_aware_text_and_stored_once(self):
        store_msgs(self.pd, [{"role": "user", "content": "go"}, call("t1", "bash_", command="deploy.sh --apply")])
        self.assertEqual(self.rows(), 2)
        for _ in range(3):
            msgs = load_msgs(self.pd)
            self.assertEqual(len(msgs), 3)
            self.assertIn("deploy.sh --apply", result_text(msgs))
            self.assertIn("may have partly run", result_text(msgs))
            store_msgs(self.pd, msgs)
        self.assertEqual(self.rows(), 3)

    def test_mid_history_dangle_keeps_the_row_count_across_reloads(self):
        store_msgs(self.pd, [{"role": "user", "content": "go"}, call("t1", "read_", path="a.py"), {"role": "user", "content": "continue"}])
        for _ in range(3):
            store_msgs(self.pd, load_msgs(self.pd))
        self.assertEqual(self.rows(), 3)
        merged = load_msgs(self.pd)[2]["content"]
        self.assertIn("call it again", merged[0]["content"])
        self.assertEqual(merged[-1], {"type": "text", "text": "continue"})

    async def test_results_are_in_postgres_before_the_next_model_call(self):
        seen = []
        inner = Scripted(turn(use("a", "get_weather", {"city": "Paris"})), final("done"))

        async def model(**kw):
            seen.append(self.rows())
            return await inner(**kw)

        with patch("micro_cc.claude_loop_.model_call", model):
            await headless.run_headless(self.pd, "weather?")
        self.assertEqual(seen, [0, 3])
        roles = [m["role"] for m in load_msgs(self.pd)]
        self.assertEqual(roles, ["user", "assistant", "user", "assistant"])

    async def test_killed_run_resumes_with_no_duplicate_rows(self):
        store_msgs(self.pd, [{"role": "user", "content": "task 7"}, call("t1", "bash_", command="deploy.sh --apply")])
        model = Scripted(final("handled"))
        with patch("micro_cc.claude_loop_.model_call", model):
            await headless.run_headless(self.pd, "continue task 7")
        sent = [b for m in model.inputs[0] if isinstance(m.get("content"), list) for b in m["content"] if b.get("type") == "tool_result"]
        self.assertIn("Call: bash_(", sent[0]["content"])
        self.assertEqual([m["role"] for m in load_msgs(self.pd)], ["user", "assistant", "user", "user", "assistant"])
        self.assertEqual(self.rows(), 5)

    def test_checkpoint_upsert_replaces_and_erases(self):
        store_checkpoint(self.pd, "first", 3, 10)
        store_checkpoint(self.pd, "second", 5, 20)
        self.assertEqual(load_checkpoint(self.pd), {"content": "second", "as_of_index": 5, "folded_tokens": 20})
        msg_store_.erase_checkpoint(self.pd)
        self.assertIsNone(load_checkpoint(self.pd))


if __name__ == "__main__":
    unittest.main()
