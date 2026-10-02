"""use_tool_ dispatcher tests. The model is scripted (no network, no keys); the
loop, search_tools, resolve_call, the adapters' stealth rename and the history
renderers are the production code. Mocked: model_call, MCP resolution/calls,
checkpoint/memory/token-stats storage, and one fake catalog tool (get_weather)
so no browser or other real tool runs.
"""

import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace as NS
from typing import Literal
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from micro_cc import claude_loop_ as loop_mod
from micro_cc.models import ContentBlock, Response
from micro_cc.models.anthropic import _stealth_tools, _unstealth_name
from micro_cc.models.schema import function_to_schema
from micro_cc.tools import search_tool_
from micro_cc.tools.use_tool_ import resolve_call, use_tool_
from micro_cc.utils import tokenization_simple as tok
from micro_cc.utils.history_mount_ import history_mount_
from micro_cc.utils.msg_normalize_ import normalize_message, reconstruct_message
from micro_cc.webui.history import to_ui_messages

CALLS = []


def get_weather(city: str, units: Literal["c", "f"] = "c"):
    """Current weather for a city.

    Args:
        city: city name.
        units: temperature unit.
    """
    CALLS.append((city, units))
    return f"{city} 21{units}"


WEATHER = {"func": get_weather, "schema": function_to_schema(get_weather), "search_text": "get_weather weather"}
MCP_ENTRY = {"server": {"type": "url", "url": "https://mcp.example/mcp", "name": "fakemcp"}, "search_text": "fake"}
MCP_TOOL = {"name": "ask_wiki", "description": "ask", "input_schema": {
    "type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]}}


def use(id, name, args):
    return ContentBlock(type="tool_use", id=id, name="use_tool_", input={"name": name, "args": args})


def direct(id, name, **args):
    return ContentBlock(type="tool_use", id=id, name=name, input=args)


def turn(*blocks):
    return Response(content=list(blocks), usage={"input": 10, "output": 1})


def final(text="done"):
    return Response(content=[ContentBlock(type="text", text=text)], usage={"input": 10, "output": 1})


class Scripted:
    """Fake model_call: replays responses in order, records the tools array
    (as bytes) and the transcript each call was sent."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.tools_bytes = []
        self.inputs = []

    async def __call__(self, *, input, model, endpoint, tools, thinking, stream):
        self.tools_bytes.append(json.dumps(tools, sort_keys=True))
        self.inputs.append(json.loads(json.dumps(input, default=str)))
        resp = self.responses.pop(0)

        async def gen():
            yield {"type": "response", "response": resp, "usage": resp.usage}
        return gen()


class LoopCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        CALLS.clear()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.mcp_catalog = {"fakemcp": MCP_ENTRY}
        self.mcp_call = AsyncMock(return_value="wiki answer")
        self.mcp_resolve = AsyncMock(return_value=([MCP_TOOL], {"ask_wiki": {"url": "https://mcp.example/mcp", "headers": None}}, []))
        search_tool_.MCP_RESOLVED.clear()
        for cm in (
            patch.dict(search_tool_.TOOL_CATALOG, {"get_weather": WEATHER}),
            patch.object(loop_mod, "get_effective_mcp_catalog", lambda _: self.mcp_catalog),
            patch.object(search_tool_, "get_effective_mcp_catalog", lambda _: self.mcp_catalog),
            patch.object(loop_mod, "resolve_mcp_tools", self.mcp_resolve),
            patch.object(search_tool_, "resolve_mcp_tools", self.mcp_resolve),
            patch.object(loop_mod, "call_mcp_tool", self.mcp_call),
            patch.object(loop_mod, "load_checkpoint", lambda _: None),
            patch.object(loop_mod, "load_summary", lambda _: ""),
            patch.object(loop_mod, "compact_checkpoint", AsyncMock()),
            patch.object(loop_mod, "save_token_stats", lambda _: None),
            patch.object(loop_mod.memory_store_, "list_memories", lambda **_: []),
        ):
            cm.start()
            self.addCleanup(cm.stop)
        patch.dict(loop_mod.token_stats, {"input": 0, "output": 0, "total_input": 0}).start()
        self.addCleanup(patch.stopall)

    async def run_loop(self, model, msgs, query="go", dangerous=(), on_event=None):
        events = []
        with patch.object(loop_mod, "model_call", model):
            async for ev in loop_mod.claude_loop(query, msgs, project_dir=self.tmp.name, dangerous_tools=set(dangerous)):
                events.append(ev)
                if on_event:
                    on_event(ev)
        return events

    @staticmethod
    def results(msgs):
        return [b for m in msgs if m["role"] == "user" and isinstance(m["content"], list)
                for b in m["content"] if b.get("type") == "tool_result"]


class ResolveCallTests(unittest.TestCase):
    def test_wrap_unwrap_object_and_dict(self):
        wrapped = use("a", "get_weather", {"city": "Paris"})
        self.assertEqual(resolve_call(wrapped), ("get_weather", {"city": "Paris"}))
        self.assertEqual(resolve_call({"type": "tool_use", "name": "use_tool_",
                                       "input": {"name": "browser", "args": {"code": "1"}}}),
                         ("browser", {"code": "1"}))

    def test_plain_block_unchanged_and_malformed_wrapper_flagged(self):
        plain = direct("a", "read_", path="x")
        self.assertEqual(resolve_call(plain), ("read_", {"path": "x"}))
        for bad in ({"name": "x"}, {"name": 3, "args": {}}, {"name": "x", "args": "no"}, {}):
            self.assertEqual(resolve_call(ContentBlock(type="tool_use", name="use_tool_", input=bad)),
                             ("use_tool_", {}))

    def test_schema_is_fixed_and_args_is_free_form_object(self):
        schema = function_to_schema(use_tool_)
        self.assertEqual(schema["name"], "use_tool_")
        self.assertEqual(schema["input_schema"]["properties"]["args"]["type"], "object")
        self.assertNotIn("additionalProperties", schema["input_schema"]["properties"]["args"])
        self.assertEqual(schema["input_schema"]["required"], ["name", "args"])

    def test_oauth_stealth_rename_round_trips_use_tool_(self):
        schema = function_to_schema(use_tool_)
        sent = _stealth_tools([schema])[0]
        self.assertEqual(sent["name"], "mcp__use_tool_")
        self.assertEqual(_unstealth_name(sent["name"]), "use_tool_")
        self.assertEqual(sent["input_schema"], schema["input_schema"])


class DispatcherLoopTests(LoopCase):
    async def test_add_returns_schema_text_then_use_tool_executes_and_array_is_stable(self):
        model = Scripted(
            turn(direct("t1", "search_tools", action="add", names="get_weather")),
            turn(use("t2", "get_weather", {"city": "Paris", "units": "f"})),
            final(),
        )
        msgs = []
        events = await self.run_loop(model, msgs)
        res = self.results(msgs)
        self.assertIn("get_weather", res[0]["content"])
        self.assertIn('"city"', res[0]["content"])           # schema delivered as text
        self.assertEqual(res[1]["content"], "Paris 21f")
        self.assertEqual(CALLS, [("Paris", "f")])
        # tools array byte-identical before and after the add, and has use_tool_ but not get_weather
        self.assertEqual(len(set(model.tools_bytes)), 1)
        names = [t["name"] for t in json.loads(model.tools_bytes[0])]
        self.assertIn("use_tool_", names)
        self.assertNotIn("get_weather", names)
        # UI events carry the INNER name; the transcript keeps the literal block
        call_events = [e for e in events if e["type"] == "tool_call"]
        self.assertEqual([(e["name"], e["input"]) for e in call_events][1],
                         ("get_weather", {"city": "Paris", "units": "f"}))
        self.assertEqual([e["name"] for e in events if e["type"] == "tool_result"], ["search_tools", "get_weather"])
        stored = [b for m in msgs if m["role"] == "assistant" and isinstance(m["content"], list)
                  for b in m["content"] if b["type"] == "tool_use"]
        self.assertEqual(stored[1], {"type": "tool_use", "id": "t2", "name": "use_tool_",
                                     "input": {"name": "get_weather", "args": {"city": "Paris", "units": "f"}}})

    async def test_persisted_history_round_trips_and_renders_as_inner_tool(self):
        model = Scripted(
            turn(direct("t1", "search_tools", action="add", names="get_weather")),
            turn(use("t2", "get_weather", {"city": "Rome"})),
            final(),
        )
        msgs = []
        await self.run_loop(model, msgs)
        restored = [reconstruct_message(normalize_message(m)) for m in msgs]
        literal = [b for m in restored if m["role"] == "assistant" and isinstance(m["content"], list)
                   for b in m["content"] if b.get("type") == "tool_use"]
        self.assertEqual(literal[1]["name"], "use_tool_")
        self.assertEqual(literal[1]["input"]["name"], "get_weather")
        self.assertEqual([m["name"] for m in history_mount_(restored) if m["type"] == "tool_call" and m.get("input") is not None],
                         ["search_tools", "get_weather"])
        parts = [p for m in to_ui_messages(restored) for p in m["parts"] if p["type"] == "dynamic-tool"]
        self.assertEqual([p["toolName"] for p in parts], ["search_tools", "get_weather"])
        self.assertEqual(parts[1]["input"], {"city": "Rome"})

    async def test_invalid_args_error_carries_schema_and_retry_succeeds(self):
        model = Scripted(
            turn(direct("t1", "search_tools", action="add", names="get_weather")),
            turn(use("t2", "get_weather", {"city": 5})),
            turn(use("t3", "get_weather", {"city": "Oslo"})),
            final(),
        )
        msgs = []
        await self.run_loop(model, msgs)
        res = self.results(msgs)
        self.assertTrue(res[1]["is_error"])
        self.assertIn("invalid args for 'get_weather'", res[1]["content"])
        self.assertIn('"input_schema"', res[1]["content"])
        self.assertEqual(res[2]["content"], "Oslo 21c")
        self.assertEqual(CALLS, [("Oslo", "c")])

    async def test_never_added_tool_with_valid_args_just_runs(self):
        model = Scripted(turn(use("t1", "get_weather", {"city": "Paris"})), final())
        msgs = []
        await self.run_loop(model, msgs)
        res = self.results(msgs)
        self.assertFalse(res[0].get("is_error"))
        self.assertEqual(res[0]["content"], "Paris 21c")

    async def test_never_added_tool_with_bad_args_errors_with_schema(self):
        model = Scripted(turn(use("t1", "get_weather", {})), final())
        msgs = []
        await self.run_loop(model, msgs)
        res = self.results(msgs)
        self.assertTrue(res[0]["is_error"])
        self.assertIn("invalid args for 'get_weather'", res[0]["content"])
        self.assertIn('"input_schema"', res[0]["content"])

    async def test_unknown_inner_tool_is_error_result(self):
        model = Scripted(turn(use("t1", "no_such_tool", {})), final())
        msgs = []
        await self.run_loop(model, msgs)
        res = self.results(msgs)
        self.assertTrue(res[0]["is_error"])
        self.assertIn("'no_such_tool' does not exist", res[0]["content"])

    async def test_malformed_wrapper_is_error_result(self):
        bad = ContentBlock(type="tool_use", id="t1", name="use_tool_", input={"name": "x"})
        nested = use("t2", "use_tool_", {})
        model = Scripted(turn(bad, nested), final())
        msgs = []
        await self.run_loop(model, msgs)
        res = self.results(msgs)
        self.assertTrue(all(r["is_error"] for r in res))
        self.assertIn("use_tool_ needs name", res[0]["content"])

    async def test_restart_keeps_array_identical(self):
        first = Scripted(
            turn(direct("t1", "search_tools", action="add", names="get_weather")),
            turn(use("t2", "get_weather", {"city": "Paris"})),
            final(),
        )
        msgs = []
        await self.run_loop(first, msgs)
        # "Restart": a fresh claude_loop call (nothing carried over but msgs).
        second = Scripted(turn(use("t3", "get_weather", {"city": "Rome"})), final())
        await self.run_loop(second, msgs, query="again")
        self.assertEqual(set(first.tools_bytes), set(second.tools_bytes))
        res = self.results(msgs)
        self.assertEqual(res[-1]["content"], "Rome 21c")   # no re-add needed
        self.assertFalse(res[-1].get("is_error"))

    async def test_dangerous_and_ask_user_use_inner_name(self):
        approvals = []

        def approve(ev):
            if ev["type"] == "approval_request":
                approvals.append((ev["name"], ev["input"]))
                ev["approval"]["approved"] = True

        model = Scripted(
            turn(direct("t1", "search_tools", action="add", names="get_weather")),
            turn(use("t2", "get_weather", {"city": "Paris"})),
            final(),
        )
        await self.run_loop(model, [], dangerous={"get_weather"}, on_event=approve)
        self.assertEqual(approvals, [("get_weather", {"city": "Paris"})])
        # gating the dispatcher's own name does nothing: the inner name is what counts
        approvals.clear()
        model = Scripted(
            turn(direct("t1", "search_tools", action="add", names="get_weather")),
            turn(use("t2", "get_weather", {"city": "Paris"})),
            final(),
        )
        await self.run_loop(model, [], dangerous={"use_tool_"}, on_event=approve)
        self.assertEqual(approvals, [])

        asked = []

        def answer(ev):
            if ev["type"] == "question_asked":
                asked.append(ev["name"])
                ev["answered"]["answered"] = {"a": 1}

        q = {"question": "ok?", "header": "h", "options": [], "multiSelect": False}
        model = Scripted(turn(use("t1", "ask_user_question_tool_", {"questions": [q]})), final())
        msgs = []
        await self.run_loop(model, msgs, on_event=answer)
        self.assertEqual(asked, ["ask_user_question_tool_"])
        bad = Scripted(turn(use("t1", "ask_user_question_tool_", {"questions": "nope"})), final())
        msgs = []
        await self.run_loop(bad, msgs, on_event=answer)
        self.assertTrue(self.results(msgs)[0]["is_error"])

    def test_batching_uses_inner_name(self):
        read = use("a", "read_", {"path": "x"})
        read2 = use("b", "read_", {"path": "y"})
        bash = use("c", "bash_", {"command": "ls"})
        add = use("d", "search_tools", {"action": "add", "names": "x"})
        batches = loop_mod._partition_tool_batches([(read, False), (read2, False), (bash, False), (add, False)])
        self.assertEqual([(safe, [b.id for b, _ in blocks]) for safe, blocks in batches],
                         [(True, ["a", "b"]), (False, ["c"]), (False, ["d"])])
        self.assertTrue(loop_mod._is_concurrency_safe(*resolve_call(use("f", "search_tools", {"action": "discover"}))))
        self.assertFalse(loop_mod._is_concurrency_safe(*resolve_call(add)))

    async def test_mcp_add_returns_schema_text_and_use_tool_routes_to_server(self):
        model = Scripted(
            turn(direct("t1", "search_tools", action="add", names="fakemcp")),
            turn(use("t2", "ask_wiki", {"q": "why"})),
            turn(use("t3", "ask_wiki", {"q": 1})),
            final(),
        )
        msgs = []
        await self.run_loop(model, msgs)
        res = self.results(msgs)
        self.assertIn("### ask_wiki", res[0]["content"])
        self.assertIn('"input_schema"', res[0]["content"])
        self.assertEqual(res[1]["content"], "wiki answer")
        self.mcp_call.assert_awaited_once_with("https://mcp.example/mcp", "ask_wiki", {"q": "why"}, headers=None)
        self.assertTrue(res[2]["is_error"])
        self.assertEqual(len(set(model.tools_bytes)), 1)

    async def test_mcp_after_restart_re_resolves_lazily_from_transcript(self):
        first = Scripted(turn(direct("t1", "search_tools", action="add", names="fakemcp")), final())
        msgs = []
        await self.run_loop(first, msgs)
        # fresh process: no resolved servers anywhere
        search_tool_.MCP_RESOLVED.clear()
        self.mcp_resolve.reset_mock()
        second = Scripted(turn(use("t2", "ask_wiki", {"q": "again"})), final())
        await self.run_loop(second, msgs, query="more")
        self.mcp_resolve.assert_awaited_once()
        self.assertEqual(self.results(msgs)[-1]["content"], "wiki answer")


class CacheSegmentTests(unittest.TestCase):
    def setUp(self):
        for name, value in (("_last_call_time", None), ("_last_model", None)):
            patch.object(tok, name, value).start()
        patch.object(tok, "_segment_hashes", []).start()
        patch.dict(tok.token_stats, {"input": 100_000}).start()
        self.addCleanup(patch.stopall)

    def usage(self):
        return {"input": 100_000, "cache_read": 0}

    def test_reports_which_segment_changed(self):
        tok.record_prompt_segments([{"name": "a"}], [{"content": "sys"}])
        self.assertIsNone(tok.detect_cache_miss(self.usage(), "m"))      # first call: nothing to compare
        tok.record_prompt_segments([{"name": "a"}, {"name": "b"}], [{"content": "sys"}])
        self.assertEqual(tok.detect_cache_miss(self.usage(), "m")["changed_segments"], ["tools"])
        tok.record_prompt_segments([{"name": "a"}, {"name": "b"}], [{"content": "sys2"}])
        self.assertEqual(tok.detect_cache_miss(self.usage(), "m")["changed_segments"], ["system"])
        tok.record_prompt_segments([{"name": "a"}, {"name": "b"}], [{"content": "sys2"}])
        self.assertEqual(tok.detect_cache_miss(self.usage(), "m")["changed_segments"], [])


if __name__ == "__main__":
    unittest.main()
