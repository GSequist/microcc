"""amend_last_reply_: the previous final reply is revised in memory and on disk, the correction turn folds in both UIs,
and transcripts without amend markers behave exactly as before."""

from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from micro_cc.utils import amend_
from micro_cc.utils import msg_store_
from micro_cc.utils.history_mount_ import history_mount_
from micro_cc.utils.msg_normalize_ import normalize_message, reconstruct_message
from micro_cc.webui.history import to_ui_messages
from test_use_tool_dispatcher import LoopCase, Scripted, direct, final, turn, use

REPLY = "A is 5. B is 7."
HISTORY = [{"role": "user", "content": "what are A and B?"}, {"role": "assistant", "content": REPLY}]


class UnitTests(unittest.TestCase):
    def test_target_is_the_previous_final_reply_past_hidden_rows(self):
        msgs = HISTORY + [{"role": "user", "content": "<system-reminder>x</system-reminder>"}]
        self.assertEqual(amend_.find_target(msgs, len(msgs)), 1)

    def test_no_target_after_a_tool_turn_or_unanswered_user_message(self):
        tool_end = [{"role": "assistant", "content": [{"type": "tool_use", "id": "t", "name": "bash_", "input": {}}]}]
        self.assertIsNone(amend_.find_target(tool_end, 1))
        self.assertIsNone(amend_.find_target(HISTORY + [{"role": "user", "content": "hi"}], 3))
        self.assertIsNone(amend_.find_target([], 0))

    def test_apply_requires_one_exact_match_and_keeps_thinking(self):
        self.assertIn("not found", amend_.apply(HISTORY[1], "C", "D")[1])
        self.assertIn("2 times", amend_.apply(HISTORY[1], " is ", "=")[1])
        think = {"type": "thinking", "thinking": "t", "signature": "s", "origin": "o"}
        blocks = {"role": "assistant", "content": [think, {"type": "text", "text": REPLY}]}
        amended, err = amend_.apply(blocks, "B is 7", "B is 8")
        self.assertEqual((err, amended["content"], amended[amend_.AMENDED_KEY]), ("", [think, {"type": "text", "text": "A is 5. B is 8."}], 1))
        side = {"role": "assistant", "content": REPLY, "_thinking": [think]}
        amended, _ = amend_.apply(side, "B is 7", "B is 8")
        self.assertEqual((amended["content"], amended["_thinking"]), ("A is 5. B is 8.", [think]))

    def test_reply_thinking_replays_only_to_its_origin(self):
        from micro_cc.models.client import drop_foreign_thinking
        think = {"type": "thinking", "thinking": "t", "signature": "s", "origin": "anthropic/m/h"}
        msg = {"role": "assistant", "content": "A is 5. B is 8.", "_thinking": [think]}
        same = drop_foreign_thinking([msg], "anthropic/m/h")[0]
        self.assertEqual(same, {"role": "assistant", "content": [think, {"type": "text", "text": "A is 5. B is 8."}]})
        other = drop_foreign_thinking([msg], "openai/x/h")[0]
        self.assertEqual(other, {"role": "assistant", "content": "A is 5. B is 8."})
        self.assertIn("_thinking", msg)  # stored row untouched

    def test_stored_thinking_renders_in_tui_history(self):
        think = {"type": "thinking", "thinking": "why B", "signature": "s"}
        rows = history_mount_([{"role": "assistant", "content": "B", "_thinking": [think]},
                               {"role": "assistant", "content": [think, {"type": "text", "text": "x"}]}])
        self.assertEqual([r["type"] for r in rows], ["thinking", "text", "thinking", "text"])

    def test_markers_survive_store_round_trip(self):
        row = {"role": "assistant", "content": "x", amend_.AMENDED_KEY: 2, amend_.ACK_KEY: True}
        back = reconstruct_message(normalize_message(row))
        self.assertEqual((back[amend_.AMENDED_KEY], back[amend_.ACK_KEY]), (2, True))

    def test_unmarked_history_renders_as_before(self):
        rows = history_mount_(HISTORY)
        self.assertEqual(rows, [{"type": "user", "content": "what are A and B?"}, {"type": "text", "content": REPLY}])
        self.assertTrue(all("metadata" not in m for m in to_ui_messages(HISTORY)))


class LiteLLMStreamTests(unittest.IsolatedAsyncioTestCase):
    async def test_signed_block_after_deltas_is_kept_once(self):
        """Gateway shape: text deltas in reasoning_content+thinking_blocks, then the full block with its signature."""
        from types import SimpleNamespace as NS
        from micro_cc.models.adapters.chat import stream_response

        def chunk(rc=None, tbs=None, content=None):
            delta = NS(content=content, reasoning_content=rc, thinking_blocks=tbs, tool_calls=None)
            return NS(choices=[NS(delta=delta, finish_reason=None)], usage=None)

        async def raw():
            for part in ("why ", "B"):
                yield chunk(rc=part, tbs=[{"type": "thinking", "thinking": part}])
            yield chunk(tbs=[{"type": "thinking", "thinking": "why B", "signature": "SIG"}])
            yield chunk(content="B is 8.")

        events = [e async for e in stream_response(raw(), reasoning="litellm")]
        thinking = "".join(e["thinking"] for e in events if e["type"] == "thinking_delta")
        final = events[-1]["response"] if "response" in events[-1] else events[-1]
        blocks = [b for b in getattr(final, "content", []) if b.type == "thinking"]
        self.assertEqual(thinking, "why B")  # streamed once, no duplicate from the signed block
        self.assertEqual([(b.thinking, b.signature) for b in blocks], [("why B", "SIG")])


class AnnotateTests(unittest.TestCase):
    def test_correction_message_gets_note_only_on_the_wire(self):
        msgs = [
            {"role": "user", "content": "what are A and B?"},
            {"role": "assistant", "content": "A is 5. B is 8.", amend_.AMENDED_KEY: 1},
            {"role": "user", "content": "B is wrong"},
            {"role": "assistant", "content": [{"type": "tool_use", "id": "a1", "name": "amend_last_reply_",
                                               "input": {"old_str": "B is 7", "new_str": "B is 8"}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "a1", "content": amend_.AMEND_OK + ": ..."}]},
            {"role": "assistant", "content": "Fixed B."},
        ]
        out = amend_.annotate(msgs)
        self.assertTrue(out[2]["content"].startswith("<system-reminder>Because of the user message below"))
        self.assertIn('"B is 7" -> "B is 8"', out[2]["content"])
        self.assertTrue(out[2]["content"].endswith("B is wrong"))
        self.assertEqual(msgs[2]["content"], "B is wrong")  # stored copy untouched
        self.assertEqual([m for i, m in enumerate(out) if i != 2], [m for i, m in enumerate(msgs) if i != 2])

    def test_failed_amend_adds_no_note(self):
        msgs = HISTORY + [{"role": "user", "content": "hm"},
                          {"role": "assistant", "content": [{"type": "tool_use", "id": "a1", "name": "amend_last_reply_", "input": {}}]},
                          {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "a1", "content": "Error: old_str not found"}]}]
        self.assertIs(amend_.annotate(msgs), msgs)


class AnthropicFamilyTests(unittest.IsolatedAsyncioTestCase):
    async def test_foundry_bedrock_vertex_send_the_same_body_as_anthropic(self):
        """Same builder for the whole family: an amended reply with same-origin signed thinking goes out identically."""
        from micro_cc.models import client as client_mod
        from micro_cc.models.apis import anthropic_messages as am

        system = [{"role": "system", "content": "sys"}]
        seen = {}

        async def fake_retry(send, **_):
            send()
            return None

        class Fake:
            class beta:
                class messages:
                    @staticmethod
                    def create(**params):
                        seen[current] = {k: params[k] for k in ("messages", "system", "thinking")}

        for current in ("anthropic", "foundry", "bedrock", "vertex"):
            origin = client_mod._origin(current, "m", system)
            think = {"type": "thinking", "thinking": "why", "signature": "SIG", "id": "", "origin": origin}
            msgs = system + [
                {"role": "user", "content": "q"},
                {"role": "assistant", "content": "A is 5. B is 8.", "_thinking": [think], amend_.AMENDED_KEY: 1},
                {"role": "user", "content": "B is wrong"},
            ]
            with patch("micro_cc.models.catalog.resolve", lambda a, p: ("m", "anthropic-messages")), \
                 patch.object(client_mod, "_alias", lambda m, p: "sonnet-5.5"), \
                 patch.object(am, "client_for", lambda *a, **k: Fake), \
                 patch.object(am, "ensure_package", lambda p: None), \
                 patch.object(am, "retry_call", fake_retry):
                await client_mod.model_call(msgs, "sonnet-5.5", provider=current, thinking=True)

        self.assertEqual(set(seen), {"anthropic", "foundry", "bedrock", "vertex"})
        reply = seen["anthropic"]["messages"][1]["content"]
        self.assertEqual([b["type"] for b in reply], ["thinking", "text"])
        self.assertEqual(reply[0], {"type": "thinking", "thinking": "why", "signature": "SIG"})  # origin/id stripped
        for p in ("foundry", "bedrock", "vertex"):
            self.assertEqual(seen[p], seen["anthropic"], p)


class AmendLoopTests(LoopCase):
    def setUp(self):
        super().setUp()
        self.home = tempfile.TemporaryDirectory()
        self.addCleanup(self.home.cleanup)
        patch.object(msg_store_.Path, "home", lambda: Path(self.home.name)).start()
        msg_store_.erase_msgs(self.tmp.name)
        msg_store_.store_msgs(self.tmp.name, HISTORY)
        self.msgs = msg_store_.load_msgs(self.tmp.name)

    async def test_amend_rewrites_reply_everywhere_and_folds_the_turn(self):
        model = Scripted(turn(direct("a", "amend_last_reply_", old_str="B is 7", new_str="B is 8")), final("Fixed B."))
        events = await self.run_loop(model, self.msgs, query="B is wrong")
        msg_store_.store_msgs(self.tmp.name, self.msgs)  # the UI's store at done

        self.assertIn({"type": "amended"}, events)
        self.assertEqual(self.msgs[1]["content"], "A is 5. B is 8.")
        self.assertTrue(self.msgs[-1][amend_.ACK_KEY])
        self.assertIn("A is 5. B is 8.", str(model.inputs[1]))  # the next call already sees the fix
        self.assertIn("you amended your previous reply in place", str(model.inputs[1]))  # and the note explaining it

        stored = msg_store_.load_msgs(self.tmp.name)
        self.assertEqual((stored[1]["content"], stored[1][amend_.AMENDED_KEY]), ("A is 5. B is 8.", 1))
        self.assertEqual(len(stored), len(self.msgs))

        rows = history_mount_(stored)
        self.assertEqual([r["type"] for r in rows], ["user", "text", "folded"])
        self.assertEqual((rows[1]["amended"], rows[2]["note"], rows[2]["ack"]), (1, "B is wrong", "Fixed B."))

        ui = to_ui_messages(stored)
        self.assertEqual(ui[1]["metadata"], {"amended": 1})
        self.assertEqual(ui[2]["parts"][0], {"type": "data-folded", "data": {"note": "B is wrong", "ack": "Fixed B."}})
        self.assertEqual(len(ui), 3)

    async def test_failed_amend_changes_nothing_and_does_not_fold(self):
        model = Scripted(turn(direct("a", "amend_last_reply_", old_str="C is 9", new_str="x")), final("ok"))
        events = await self.run_loop(model, self.msgs, query="hm")
        self.assertNotIn({"type": "amended"}, events)
        self.assertIn("not found", self.results(self.msgs)[0]["content"])
        self.assertEqual(msg_store_.load_msgs(self.tmp.name)[1]["content"], REPLY)
        self.assertNotIn(amend_.ACK_KEY, self.msgs[-1])

    async def test_turn_with_other_work_is_not_folded(self):
        model = Scripted(
            turn(direct("a", "amend_last_reply_", old_str="B is 7", new_str="B is 8"), use("b", "get_weather", {"city": "Paris"})),
            final("Fixed B and checked the weather."),
        )
        await self.run_loop(model, self.msgs, query="fix B and get weather")
        self.assertEqual(self.msgs[1]["content"], "A is 5. B is 8.")
        self.assertNotIn(amend_.ACK_KEY, self.msgs[-1])


if __name__ == "__main__":
    unittest.main()
