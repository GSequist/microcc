"""Final replies keep their thinking beside the string text; same-origin thinking replays, foreign is dropped,
history shows it, LiteLLM streams keep the signed block, and the Anthropic-format family sends one body."""

from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from micro_cc.models import ContentBlock, Response
from micro_cc.models.client import drop_foreign_thinking
from micro_cc.utils.history_mount_ import history_mount_
from micro_cc.utils.msg_normalize_ import normalize_message, reconstruct_message
from micro_cc.webui.history import to_ui_messages
from test_use_tool_dispatcher import LoopCase, Scripted

REPLY = "A is 5. B is 8."
HISTORY = [{"role": "user", "content": "what are A and B?"}, {"role": "assistant", "content": REPLY}]
THINK = {"type": "thinking", "thinking": "why B", "signature": "SIG", "id": "", "origin": "anthropic/m/h"}


class ReplyThinkingTests(unittest.TestCase):
    def test_replays_only_to_its_origin(self):
        msg = {"role": "assistant", "content": REPLY, "_thinking": [THINK]}
        same = drop_foreign_thinking([msg], "anthropic/m/h")[0]
        self.assertEqual(same, {"role": "assistant", "content": [THINK, {"type": "text", "text": REPLY}]})
        other = drop_foreign_thinking([msg], "openai/x/h")[0]
        self.assertEqual(other, {"role": "assistant", "content": REPLY})
        self.assertIn("_thinking", msg)  # stored row untouched

    def test_survives_store_round_trip(self):
        back = reconstruct_message(normalize_message({"role": "assistant", "content": REPLY, "_thinking": [THINK]}))
        self.assertEqual((back["content"], back["_thinking"]), (REPLY, [THINK]))

    def test_renders_in_tui_and_web_history(self):
        msgs = [{"role": "assistant", "content": "B", "_thinking": [THINK]},
                {"role": "assistant", "content": [THINK, {"type": "text", "text": "x"}]}]
        self.assertEqual([r["type"] for r in history_mount_(msgs)], ["thinking", "text", "thinking", "text"])
        self.assertEqual([p["type"] for p in to_ui_messages(msgs)[0]["parts"]], ["reasoning", "text"])

    def test_history_without_thinking_renders_as_before(self):
        self.assertEqual(history_mount_(HISTORY), [{"type": "user", "content": "what are A and B?"}, {"type": "text", "content": REPLY}])
        self.assertEqual([m["parts"][0]["type"] for m in to_ui_messages(HISTORY)], ["text", "text"])


class LoopStoresReplyThinkingTests(LoopCase):
    async def test_final_reply_is_a_string_with_thinking_beside_it(self):
        reply = Response(content=[ContentBlock(type="thinking", thinking="why", signature="SIG"), ContentBlock(type="text", text=REPLY)],
                         usage={"input": 10, "output": 1})
        msgs = []
        await self.run_loop(Scripted(reply), msgs)
        last = msgs[-1]
        self.assertEqual(last["content"], REPLY)
        self.assertEqual([(b["thinking"], b["signature"]) for b in last["_thinking"]], [("why", "SIG")])
        self.assertTrue(last["_thinking"][0]["origin"])


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
            yield chunk(content=REPLY)

        events = [e async for e in stream_response(raw(), reasoning="litellm")]
        thinking = "".join(e["thinking"] for e in events if e["type"] == "thinking_delta")
        blocks = [b for b in events[-1]["response"].content if b.type == "thinking"]
        self.assertEqual(thinking, "why B")  # streamed once, no duplicate from the signed block
        self.assertEqual([(b.thinking, b.signature) for b in blocks], [("why B", "SIG")])


class AnthropicFamilyTests(unittest.IsolatedAsyncioTestCase):
    async def test_foundry_bedrock_vertex_send_the_same_body_as_anthropic(self):
        """Same builder for the whole family: a reply with same-origin signed thinking goes out identically."""
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
            think = {"type": "thinking", "thinking": "why", "signature": "SIG", "id": "", "origin": client_mod._origin(current, "m", system)}
            msgs = system + [
                {"role": "user", "content": "q"},
                {"role": "assistant", "content": REPLY, "_thinking": [think]},
                {"role": "user", "content": "next"},
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


if __name__ == "__main__":
    unittest.main()
