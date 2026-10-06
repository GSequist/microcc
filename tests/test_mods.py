"""Mods: loader isolation, middleware chain, render/glyph/command/key events, legacy sweep, reload manifest."""
import asyncio
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from micro_cc import mods_  # noqa: E402


class ModsCase(unittest.TestCase):
    def setUp(self):
        self._old = os.environ.get("HOME")
        self.home = tempfile.mkdtemp(prefix="mcc_mods_")
        os.environ["HOME"] = self.home
        self.root = Path(self.home) / ".micro-cc"
        self.notes = []
        mods_.bind(lambda: SimpleNamespace(notify=self.notes.append))
        mods_.clear_errors()
        mods_.load()

    def tearDown(self):
        os.environ["HOME"] = self.home
        mods_.clear_errors()
        for p in sorted((self.root / "mods").glob("**/*"), reverse=True) if (self.root / "mods").exists() else []:
            p.rmdir() if p.is_dir() else p.unlink()
        mods_.load()
        from micro_cc.tui_native import glyphs_
        glyphs_.reload()
        if self._old is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = self._old

    def _w(self, rel, text):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        return p

    def _errs(self):
        return " ".join(mods_.errors())


class TestLoader(ModsCase):
    def test_broken_mods_isolated(self):
        self._w("mods/good/mod.py", "from .parts import X\ndef register(on):\n    @on('glyphs')\n    def g(api, e, nxt): return nxt(e)\n")
        self._w("mods/good/parts.py", "X = 1\n")
        self._w("mods/raises/mod.py", "raise RuntimeError('boom')\n")
        self._w("mods/exits/mod.py", "import sys; sys.exit(3)\n")
        self._w("mods/syntax/mod.py", "def (:\n")
        self._w("mods/nomodpy/other.py", "x = 1\n")
        self._w("mods/oldapi/mod.py", "API_VERSION = 1\ndef register(on): pass\n")
        self._w("mods/badevent/mod.py", "def register(on):\n    on('ui.render')(lambda a, e, n: n(e))\n")
        self._w("mods/badcmd/mod.py", "def register(on):\n    on('command', name='Bad Name')(lambda a, e, n: None)\n")
        self._w("mods/ctrlc/mod.py", "def register(on):\n    on('key', key='ctrl+c')(lambda a, e, n: True)\n")
        self._w("mods/noreg/mod.py", "x = 1\n")
        self._w("mods/_off/mod.py", "raise RuntimeError('never imported')\n")
        self.assertEqual(mods_.load(), ["good"])
        errs = self._errs()
        for name in ("raises", "exits", "syntax", "nomodpy", "oldapi", "badevent", "badcmd", "ctrlc", "noreg"):
            self.assertIn(f"mods/{name}", errs)
        self.assertNotIn("_off", errs)

    def test_missing_dir(self):
        self.assertEqual(mods_.load(), [])
        self.assertEqual(mods_.errors(), [])


class TestChain(ModsCase):
    def test_order_wrap_replace_and_failure(self):
        # a loads before b, so a is outermost.
        self._w("mods/a/mod.py", "def register(on):\n    @on('render', component='statusbar')\n"
                "    def wrap(api, e, nxt): return ['A', *nxt(e)]\n")
        self._w("mods/b/mod.py", "def register(on):\n    @on('render', component='statusbar')\n"
                "    def edit(api, e, nxt): return nxt({**e, 'markup': e['markup'].upper()})\n")
        mods_.load()
        self.assertEqual(mods_.render_markup("statusbar", "x", 40), "A\nX")
        self.assertEqual(mods_.render_markup("hintbar", "x", 40), "x")  # unmatched component untouched
        self._w("mods/b/mod.py", "def register(on):\n    @on('render', component='statusbar')\n"
                "    def boom(api, e, nxt): raise ValueError('bad')\n")
        mods_.load()
        for _ in range(3):
            self.assertEqual(mods_.render_markup("statusbar", "x", 40), "A\nx")
        self.assertEqual(sum("mods/b" in e for e in mods_.errors()), 1)
        self.assertFalse(mods_.has("render", component="hintbar"))
        self.assertTrue(mods_.has("render", component="statusbar"))

    def test_slow_handler_disabled(self):
        self._w("mods/slow/mod.py", "import time\ndef register(on):\n    @on('render', component='statusbar')\n"
                "    def s(api, e, nxt):\n        time.sleep(0.03)\n        return ['slow']\n")
        self._w("mods/wraps_slow/mod.py", "def register(on):\n    @on('render', component='statusbar')\n"
                "    def w(api, e, nxt): return nxt(e)\n")
        mods_.load()
        old = mods_.SLOW_S
        mods_.SLOW_S = 0.01
        try:
            self.assertEqual(mods_.render_markup("statusbar", "x", 40), "slow")
            self.assertEqual(mods_.render_markup("statusbar", "x", 40), "x")
        finally:
            mods_.SLOW_S = old
        errs = self._errs()
        self.assertIn("mods/slow", errs)
        self.assertNotIn("mods/wraps_slow", errs)  # time spent inside nxt is not its own

    def test_invalid_markup_falls_back(self):
        self._w("mods/m/mod.py", "def register(on):\n    @on('render', component='statusbar')\n"
                "    def bad(api, e, nxt): return '[/nope]'\n")
        mods_.load()
        self.assertEqual(mods_.render_markup("statusbar", "ok", 40), "ok")
        self.assertIn("render statusbar", self._errs())


class TestWatchdog(ModsCase):
    def setUp(self):
        super().setUp()
        self._old_hang = (mods_.HANG_S, mods_.LOAD_HANG_S, mods_.SLOW_S)
        mods_.HANG_S, mods_.LOAD_HANG_S, mods_.SLOW_S = 0.05, 0.2, 10

    def tearDown(self):
        mods_.HANG_S, mods_.LOAD_HANG_S, mods_.SLOW_S = self._old_hang
        import signal
        self.assertEqual(signal.getitimer(signal.ITIMER_REAL)[0], 0)  # nothing left armed
        self.assertEqual(signal.getsignal(signal.SIGALRM), signal.SIG_DFL)
        super().tearDown()

    def test_hung_handlers_interrupted_and_blamed(self):
        self._w("mods/a_outer/mod.py", "def register(on):\n    @on('render', component='statusbar')\n"
                "    def w(api, e, nxt): return ['A', *nxt(e)]\n")
        self._w("mods/b_loop/mod.py", "def register(on):\n    @on('render', component='statusbar')\n"
                "    def spin(api, e, nxt):\n        while True: pass\n")
        self._w("mods/c_swallow/mod.py", "import time\ndef register(on):\n    @on('render', component='hintbar')\n"
                "    def s(api, e, nxt):\n        try:\n            time.sleep(5)\n        except Exception:\n"
                "            return ['swallowed']\n")
        mods_.load()
        t = time.perf_counter()
        self.assertEqual(mods_.render_markup("statusbar", "x", 40), "A\nx")
        self.assertEqual(mods_.render_markup("hintbar", "h", 40), "h")
        self.assertLess(time.perf_counter() - t, 1.0)
        errs = self._errs()
        self.assertIn("mods/b_loop", errs)
        self.assertIn("mods/c_swallow", errs)
        self.assertNotIn("mods/a_outer", errs)  # its clock was paused while b ran

    def test_slow_builtin_not_blamed(self):
        self._w("mods/w/mod.py", "def register(on):\n    @on('render', component='statusbar')\n"
                "    def w(api, e, nxt): return nxt(e)\n")
        mods_.load()

        def slow_builtin(e):
            time.sleep(0.15)
            return "done"
        self.assertEqual(mods_.dispatch("render", {}, slow_builtin, component="statusbar"), "done")
        self.assertEqual(mods_.errors(), [])

    def test_hung_import_and_sync_command(self):
        self._w("mods/hangs/mod.py", "while True: pass\n")
        self._w("mods/cmd/mod.py", "import time\ndef register(on):\n    @on('command', name='stuck')\n"
                "    def stuck(api, e, nxt): time.sleep(5)\n")
        self.assertEqual(mods_.load(), ["cmd"])
        self.assertIn("loading took over", self._errs())
        with self.assertRaises(RuntimeError):
            asyncio.run(mods_.adispatch("command", {"name": "stuck"}, lambda e: None, name="stuck"))
        self.assertIsNone(mods_.match_command("/stuck"))


class TestRender(ModsCase):
    def test_anchor_cache_and_bump(self):
        from micro_cc.tui_native.message_row_ import RichStatic
        self._w("mods/ctx/mod.py", "n = [0]\ndef register(on):\n    @on('render', component='above_input')\n"
                "    def ctx(api, e, nxt):\n        n[0] += 1\n        return [*nxt(e), f'ctx {n[0]}']\n")
        mods_.load()
        anchor, plain = RichStatic("", name="above_input"), RichStatic("")
        self.assertEqual(plain.render(30), [])
        self.assertIn("ctx 1", anchor.render(30)[0])
        self.assertIn("ctx 1", anchor.render(30)[0])  # cached
        mods_.bump()
        self.assertIn("ctx 2", anchor.render(30)[0])

    def test_message_wrap_by_type_and_tool(self):
        from micro_cc.tui_native import renderers_
        self._w("mods/m/mod.py", (
            "def register(on):\n"
            "    @on('render', component='message', type='user')\n"
            "    def u(api, e, nxt): return e['r'].Text('U ' + e['msg']['content'])\n"
            "    @on('render', component='message', type='tool_call', tool='bash_')\n"
            "    def b(api, e, nxt): return e['r'].Group(e['r'].Text('BASH'), nxt(e))\n"
            "    @on('render', component='message', type='error')\n"
            "    def boom(api, e, nxt): raise ValueError('x')\n"))
        mods_.load()
        self.assertEqual(renderers_.render_msg({"type": "user", "content": "hi"}).plain, "U hi")
        t = {"type": "tool_call", "name": "bash_", "input": {"command": "ls"}, "result": "ok"}
        self.assertEqual(renderers_.render_msg(t).renderables[0].plain, "BASH")
        self.assertIn("read_", renderers_.render_msg(dict(t, name="read_")).plain)
        self.assertIn("bad", renderers_.render_msg({"type": "error", "content": "bad"}).plain)
        self.assertIn("mods/m", self._errs())

    def test_mod_cannot_mutate_stored_row(self):
        from micro_cc.tui_native import renderers_
        self._w("mods/m/mod.py", "def register(on):\n    @on('render', component='message', type='tool_call')\n"
                "    def m(api, e, nxt):\n        e['msg']['input']['command'] = 'x'\n        e['msg']['result'] = 'HACKED'\n        return nxt(e)\n")
        mods_.load()
        row = {"type": "tool_call", "name": "bash_", "input": {"command": "ls"}, "result": "ok"}
        self.assertIn("HACKED", renderers_.render_msg(row).plain)  # the mod's edit renders
        self.assertEqual((row["input"]["command"], row["result"]), ("ls", "ok"))  # stored row untouched


class TestGlyphs(ModsCase):
    def test_override_partial_and_invalid(self):
        from micro_cc.tui_native import glyphs_
        from micro_cc.tui_native.message_row_ import MessageRow
        from micro_cc.tui_native.prompt_input_ import PromptInput
        self._w("mods/g/mod.py", "def register(on):\n    @on('glyphs')\n"
                "    def g(api, e, nxt): return {**nxt(e), 'user_prompt': '>>', 'bogus': 'x', 'tool': ''}\n")
        mods_.load()
        glyphs_.reload()
        self.assertEqual(glyphs_.glyph("user_prompt"), ">>")
        self.assertEqual(glyphs_.glyph("tool"), glyphs_.DEFAULTS["tool"])
        self.assertEqual(len(mods_.errors()), 2)
        self.assertIn(">> hi", MessageRow({"type": "user", "content": "hi"}).render(40)[0])
        p = PromptInput()
        p.insert("x")
        self.assertIn(">>", p.render(20)[0])


class TestCommands(ModsCase):
    def _mods(self):
        self._w("mods/c/mod.py", (
            "def register(on):\n"
            "    @on('command', name='/standup', hint='daily')\n"
            "    def s(api, e, nxt): api.send_prompt('Summarize: ' + e['arg'])\n"
            "    @on('command', name='ok')\n"
            "    async def ok(api, e, nxt): api.notify('ran ' + e['arg'])\n"
            "    @on('command', name='clear', hint='mine')\n"
            "    async def c(api, e, nxt):\n        api.notify('before')\n        await nxt(e)\n"))
        self._w("mods/d/mod.py", "def register(on):\n    @on('command', name='boom')\n"
                "    async def b(api, e, nxt): raise ValueError('bad')\n")
        mods_.load()

    def test_listing_and_match(self):
        from micro_cc.utils import command_registry
        self._mods()
        info = dict(mods_.slash_command_info())
        self.assertTrue({c["name"] for c in command_registry.COMMANDS} <= set(info))
        self.assertEqual(info["/standup"], "daily")
        self.assertEqual(info["/clear"], "(mod) mine")
        self.assertEqual(len(mods_.slash_command_info()), len(info))
        self.assertEqual(mods_.match_command("/standup the week"), ("standup", "the week"))
        self.assertIsNone(mods_.match_command("/standups"))
        self.assertIsNone(mods_.match_command("/model"))

    def test_dispatch_through_submit(self):
        import micro_cc.start_live_tui_ as live
        from micro_cc.tui_native.prompt_input_ import PromptInput
        self._mods()
        rows, sent, builtin_runs = [], [], []

        def make(mode="idle"):
            app = SimpleNamespace(prompt=PromptInput(), _input_mode=mode, _gui_url="", _queue=[],
                                  _flash_status=lambda t, seconds=3: self.notes.append(t))

            async def mount(msg):
                rows.append(msg)

            async def submit(t, literal=False, skip_mods=False):
                if skip_mods:
                    builtin_runs.append(t)
                else:
                    await live.MicroTui._on_prompt_submitted(app, t, literal, skip_mods)
            app._mount_row = mount
            app._on_prompt_submitted = submit
            return app

        mods_.bind(lambda: SimpleNamespace(notify=self.notes.append, send_prompt=sent.append))
        asyncio.run(live.MicroTui._on_prompt_submitted(make(), "/ok now"))
        self.assertIn("ran now", self.notes)
        asyncio.run(live.MicroTui._on_prompt_submitted(make("query_active"), "/standup bob"))
        self.assertEqual(sent, ["Summarize: bob"])  # mod commands run mid-stream
        asyncio.run(live.MicroTui._on_prompt_submitted(make(), "/clear"))
        self.assertEqual((self.notes[-1], builtin_runs), ("before", ["/clear"]))
        asyncio.run(live.MicroTui._on_prompt_submitted(make(), "/boom"))
        self.assertTrue(any(r["type"] == "error" and "/boom failed" in r["content"] for r in rows))
        self.assertIsNone(mods_.match_command("/boom"))  # disabled
        self.assertIn("mods/d", self._errs())


class TestKeys(ModsCase):
    def test_consume_and_reserved(self):
        for k in ("ctrl+c", "escape", "esc", "enter", "shift+enter", "CTRL+C"):
            self.assertTrue(mods_.is_reserved(k), k)
        for k in ("ctrl+g", "ctrl+shift+c", "ctrl+t"):
            self.assertFalse(mods_.is_reserved(k), k)
        self._w("mods/k/mod.py", "def register(on):\n    @on('key', key='ctrl+g')\n"
                "    def g(api, e, nxt):\n        api.notify('g')\n        return True\n")
        mods_.load()
        self.assertTrue(mods_.handle_key("\x07"))
        self.assertEqual(self.notes, ["g"])
        self.assertFalse(mods_.handle_key("\x14"))
        self.assertFalse(mods_.handle_key("\x03"))


class TestSweep(ModsCase):
    def test_moves_legacy_keeps_rest(self):
        self._w("commands/standup.md", "x")
        self._w("panels/p.py", "x")
        self._w("keys.json", "{}")
        self._w("banner.txt", "b")
        self._w("statusline.sh", "#!/bin/sh\necho mine\n")
        self._w("theme.json", "{}")
        self._w("legacy/keys.json", "older")
        moved = mods_.sweep_legacy()
        self.assertEqual(sorted(moved), ["banner.txt", "commands", "keys.json", "panels"])
        self.assertTrue((self.root / "legacy/commands/standup.md").exists())
        self.assertEqual((self.root / "legacy/keys.json").read_text(), "older")
        self.assertEqual(len(list((self.root / "legacy").glob("keys.json.*"))), 1)
        for kept in ("statusline.sh", "theme.json"):
            self.assertTrue((self.root / kept).exists())
        self.assertEqual(mods_.sweep_legacy(), [])


class TestManifest(ModsCase):
    def test_manifest_covers_mods_only(self):
        from micro_cc.utils import self_reload_ as sr
        self._w("mods/a/mod.py", "x")
        self._w("mods/a/data.json", "{}")
        self._w("mods/a/.mod.py.swp", "x")
        self._w("mods/a/__pycache__/mod.pyc", "x")
        self._w("mods/.hidden/mod.py", "x")
        self._w("statusline.sh", "x")
        self._w("legacy/commands/a.md", "x")
        m = sr._mods_manifest()
        self.assertEqual({os.path.relpath(p, self.root) for p in m}, {"mods/a/mod.py", "mods/a/data.json"})
        self.assertTrue(set(m) <= set(sr.build_manifest()))
        before = sr.build_manifest()
        time.sleep(0.01)
        self._w("mods/a/mod.py", "changed!")
        changed = sr.diff_manifest(before, sr.build_manifest())
        self.assertEqual({os.path.relpath(p, self.root) for p in changed}, {"mods/a/mod.py"})


class TestStatusline(ModsCase):
    def test_overlay_and_migration(self):
        from micro_cc.utils import statusline_
        from micro_cc.utils.default_status_sh import _DEFAULT_STATUS_SCRIPT
        d = statusline_.resolve()
        self.assertTrue(d.endswith("cache/statusline.default.sh"))
        self.assertEqual(Path(d).read_text(), _DEFAULT_STATUS_SCRIPT)
        self.assertTrue(os.access(d, os.X_OK))
        self.assertFalse((self.root / "statusline.sh").exists())  # no seed-copy
        Path(d).write_text("old")
        self.assertEqual(Path(statusline_.resolve()).read_text(), _DEFAULT_STATUS_SCRIPT)
        self._w("statusline.sh", _DEFAULT_STATUS_SCRIPT)
        self.assertTrue(statusline_.migrate())
        self.assertFalse((self.root / "statusline.sh").exists())
        mine = self._w("statusline.sh", "#!/bin/sh\necho mine\n")
        self.assertFalse(statusline_.migrate())
        self.assertEqual(statusline_.resolve(), str(mine))


if __name__ == "__main__":
    unittest.main()
