"""Turn recovery: a stream that dies mid-turn is re-sent once the partial turn is
discarded, and an empty turn (e.g. all reasoning cut at max_tokens) is re-prompted
instead of ending the run. Fakes only, no network.
"""

from pathlib import Path
from types import SimpleNamespace
import sys
import unittest
from unittest.mock import patch

import httpx
import openai

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from micro_cc.models import retry
from micro_cc.models.adapters import chat
from micro_cc.models.types import ContentBlock, Response
from test_use_tool_dispatcher import CALLS, LoopCase, Scripted, final, use

REQ = httpx.Request("POST", "https://example.invalid/v1/chat/completions")


def dropped():
    return openai.APIConnectionError(request=REQ)


def bad_request():
    return openai.BadRequestError("bad", response=httpx.Response(400, request=REQ), body=None)


def attempts(*scripts):
    """send/decode_stream pair replaying one event script per attempt; an exception item is raised."""
    state = {"n": 0}

    async def send():
        state["n"] += 1
        return scripts[state["n"] - 1]

    async def decode_stream(script):
        for item in script:
            if isinstance(item, BaseException):
                raise item
            yield item

    return send, decode_stream, state


async def call(send, decode_stream, retries=3):
    return await retry.retry_call(send, stream=True, decode=None, decode_stream=decode_stream,
                                  timeout=5, retries=retries, label="test")


async def drain(gen):
    return [e async for e in gen]


def text(t):
    return {"type": "text_delta", "text": t}


def resp(r):
    return {"type": "response", "response": r, "usage": r.usage}


class MidStreamRetry(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        patch.object(retry, "BASE_SLEEP", 0).start()
        self.addCleanup(patch.stopall)

    async def test_transient_drop_resets_then_replays(self):
        done = final("full answer")
        send, dec, state = attempts([text("par"), dropped()], [text("full answer"), resp(done)])
        events = await drain(await call(send, dec))
        self.assertEqual([e["type"] for e in events], ["text_delta", "stream_reset", "text_delta", "response"])
        self.assertIn("APIConnectionError", events[1]["error"])
        self.assertEqual(state["n"], 2)

    async def test_non_transient_error_is_not_retried(self):
        send, dec, state = attempts([text("par"), bad_request()], [resp(final())])
        with self.assertRaises(openai.BadRequestError):
            await drain(await call(send, dec))
        self.assertEqual(state["n"], 1)

    async def test_resets_are_bounded(self):
        send, dec, state = attempts(*[[text("x"), dropped()]] * 5)
        gen = await call(send, dec, retries=2)
        with self.assertRaises(openai.APIConnectionError):
            await drain(gen)
        self.assertEqual(state["n"], 3)  # first try + 2 re-sends

    def test_transient_classification(self):
        self.assertTrue(retry.is_transient(dropped()))
        self.assertTrue(retry.is_transient(httpx.ReadError("eof")))
        self.assertTrue(retry.is_transient(openai.InternalServerError(
            "502", response=httpx.Response(502, request=REQ), body=None)))
        self.assertTrue(retry.is_transient(openai.APIError("in-stream", request=REQ, body={"code": 502})))
        self.assertFalse(retry.is_transient(bad_request()))
        self.assertFalse(retry.is_transient(openai.AuthenticationError(
            "no", response=httpx.Response(401, request=REQ), body=None)))


class ChatFinishReason(unittest.IsolatedAsyncioTestCase):
    async def test_length_maps_to_max_tokens(self):
        async def chunks():
            delta = SimpleNamespace(content=None, tool_calls=None, reasoning="thinking...")
            yield SimpleNamespace(choices=[SimpleNamespace(delta=delta, finish_reason=None)], usage=None)
            yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=None, tool_calls=None),
                                                           finish_reason="length")], usage=None)
        events = await drain(chat.stream_response(chunks(), reasoning="openrouter"))
        r = events[-1]["response"]
        self.assertEqual(r.stop_reason, "max_tokens")
        self.assertEqual([b.type for b in r.content], ["thinking"])


def truncated():
    return Response(content=[ContentBlock(type="thinking", thinking="plan " * 50)],
                    usage={"input": 10, "output": 64000}, stop_reason="max_tokens")


class LoopRecovery(LoopCase):
    def setUp(self):
        super().setUp()
        patch.object(retry, "BASE_SLEEP", 0).start()

    async def test_empty_length_turn_is_reprompted(self):
        model = Scripted(truncated(), final("all done"))
        msgs = []
        events = await self.run_loop(model, msgs)
        types = [e["type"] for e in events]
        self.assertIn("turn_reset", types)
        self.assertNotIn("error", types)
        self.assertEqual(types[-2:], ["final_text", "done"])
        self.assertEqual(msgs[-1], {"role": "assistant", "content": "all done"})
        self.assertIn("output-token cap", str(model.inputs[1]))
        self.assertFalse(any(m["role"] == "assistant" and isinstance(m["content"], list) for m in msgs))

    async def test_empty_turns_are_bounded(self):
        from micro_cc import claude_loop_ as loop_mod
        model = Scripted(*[truncated() for _ in range(loop_mod.REPROMPT_LIMIT + 1)])
        events = await self.run_loop(model, [])
        types = [e["type"] for e in events]
        self.assertEqual(types.count("turn_reset"), loop_mod.REPROMPT_LIMIT)
        self.assertNotIn("final_text", types)
        self.assertIn("empty turns", [e for e in events if e["type"] == "error"][-1]["message"])

    async def test_mid_stream_drop_discards_partial_turn(self):
        good = Response(content=[ContentBlock(type="text", text="checking"), use("t1", "get_weather", {"city": "Rome"})],
                        usage={"input": 10, "output": 5})
        send, dec, state = attempts(
            [text("check"), {"type": "tool_use_start", "name": "use_tool_"}, dropped()],
            [text("checking"), resp(good)],
            [text("done"), resp(final("Rome is 21c"))],
        )

        async def model(**_kw):
            return await call(send, dec)

        msgs = []
        events = await self.run_loop(model, msgs)
        types = [e["type"] for e in events]
        self.assertEqual(types.count("turn_reset"), 1)
        self.assertNotIn("error", types)
        self.assertEqual(CALLS, [("Rome", "c")])  # nothing from the dropped turn ran
        assistants = [m for m in msgs if m["role"] == "assistant"]
        self.assertEqual(len(assistants), 2)
        self.assertEqual([b["text"] for b in assistants[0]["content"] if b["type"] == "text"], ["checking"])
        self.assertEqual(msgs[-1]["content"], "Rome is 21c")


if __name__ == "__main__":
    unittest.main()
