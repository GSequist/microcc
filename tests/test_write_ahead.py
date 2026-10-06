"""Write-ahead tool calls: the tool_use is flushed before any tool runs and every
call of a cut-short turn gets a result. Model scripted, store_msgs recorded
(nothing touches ~/.micro-cc), tools are fake catalog entries.
"""

import asyncio
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from micro_cc import claude_loop_ as loop_mod
from micro_cc.models.schema import function_to_schema
from micro_cc.tools import search_tool_
from test_use_tool_dispatcher import LoopCase, Scripted, final, turn, use

flush_log = []
seen_at_run = []
started_tags = []


async def quick(tag: str):
    """Finishes at once."""
    seen_at_run.append(flush_log[-1] if flush_log else None)
    return f"quick {tag}"


async def boom(tag: str):
    """Raises."""
    raise RuntimeError(f"boom {tag}")


async def hang(tag: str):
    """Never finishes."""
    started_tags.append(tag)
    await asyncio.sleep(3600)


def entry(fn):
    return {"func": fn, "schema": function_to_schema(fn), "search_text": fn.__name__}


class WriteAheadCase(LoopCase):
    def setUp(self):
        super().setUp()
        for log in (flush_log, seen_at_run, started_tags):
            log.clear()
        patch.dict(search_tool_.TOOL_CATALOG, {f.__name__: entry(f) for f in (quick, boom, hang)}).start()
        patch.object(loop_mod, "_CONCURRENCY_SAFE_TOOLS", {"quick", "boom", "hang"}).start()
        patch.object(loop_mod, "store_msgs", lambda _p, m: flush_log.append([x["role"] for x in m])).start()

    async def cancel_after_hang(self, model, msgs):
        task = asyncio.create_task(self.run_loop(model, msgs))
        for _ in range(300):
            if started_tags:
                break
            await asyncio.sleep(0.01)
        self.assertTrue(started_tags, "hang never started")
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    def assert_valid(self, msgs):
        self.assertEqual(msgs[-1]["role"], "user")
        use_ids = [b["id"] for b in msgs[-2]["content"] if b.get("type") == "tool_use"]
        self.assertEqual(use_ids, [b["tool_use_id"] for b in msgs[-1]["content"]])

    async def test_tool_use_is_flushed_before_the_tool_runs(self):
        await self.run_loop(Scripted(turn(use("a", "quick", {"tag": "1"})), final()), [])
        self.assertEqual(seen_at_run[0][-1], "assistant")

    async def test_results_are_flushed_before_the_next_model_call(self):
        inner, seen = Scripted(turn(use("a", "quick", {"tag": "1"})), final()), []

        async def model(**kw):
            seen.append(flush_log[-1][-1] if flush_log else None)
            return await inner(**kw)

        await self.run_loop(model, [])
        self.assertEqual(seen, [None, "user"])

    async def test_parallel_failure_keeps_sibling_results(self):
        msgs = []
        await self.run_loop(Scripted(turn(use("a", "quick", {"tag": "1"}), use("b", "boom", {"tag": "2"})), final()), msgs)
        by_id = {r["tool_use_id"]: r["content"] for r in self.results(msgs)}
        self.assertEqual(by_id["a"], "quick 1")
        self.assertIn("RuntimeError: boom 2", by_id["b"])

    async def test_cancel_in_parallel_batch_keeps_finished_and_marks_the_hung(self):
        msgs = []
        await self.cancel_after_hang(Scripted(turn(use("a", "quick", {"tag": "1"}), use("b", "hang", {"tag": "2"}))), msgs)
        self.assert_valid(msgs)
        by_id = {r["tool_use_id"]: r for r in self.results(msgs)}
        self.assertEqual(by_id["a"]["content"], "quick 1")
        self.assertIn("Interrupted", by_id["b"]["content"])
        self.assertTrue(by_id["b"]["is_error"])

    async def test_cancel_in_serial_run_marks_not_started(self):
        loop_mod._CONCURRENCY_SAFE_TOOLS.clear()
        model = Scripted(turn(use("a", "quick", {"tag": "1"}), use("b", "hang", {"tag": "2"}), use("c", "quick", {"tag": "3"})))
        msgs = []
        await self.cancel_after_hang(model, msgs)
        self.assert_valid(msgs)
        by_id = {r["tool_use_id"]: r["content"] for r in self.results(msgs)}
        self.assertEqual(by_id["a"], "quick 1")
        self.assertIn("Interrupted", by_id["b"])
        self.assertIn("Not run", by_id["c"])

    async def test_denied_approval_answers_every_call(self):
        def deny(ev):
            if ev["type"] == "approval_request":
                ev["approval"]["approved"] = False
        msgs = []
        await self.run_loop(Scripted(turn(use("a", "quick", {"tag": "1"}))), msgs, dangerous={"quick"}, on_event=deny)
        self.assert_valid(msgs)
        self.assertIn("Not run", self.results(msgs)[0]["content"])

    async def test_aclose_answers_open_calls(self):
        msgs = []
        with patch.object(loop_mod, "model_call", Scripted(turn(use("a", "hang", {"tag": "1"})))):
            gen = loop_mod.claude_loop("go", msgs, project_dir=self.tmp.name)
            async for ev in gen:
                if ev["type"] == "tool_call":
                    break
            await gen.aclose()
        self.assert_valid(msgs)
        self.assertIn("Not run", self.results(msgs)[0]["content"])


if __name__ == "__main__":
    unittest.main()
