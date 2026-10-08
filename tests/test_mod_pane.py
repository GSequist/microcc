"""Mod side pane: HSplit layout, failure isolation, focus/mouse routing, search+selection width, agent observe."""
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_mods import ModsCase  # noqa: E402  (also puts src/ on the path and isolates HOME)
from micro_cc import mods_  # noqa: E402
from micro_cc.tui_native import pane_  # noqa: E402
from micro_cc.tui_native.alt_screen_ import SelectionPoint, TuiAltScreen  # noqa: E402
from micro_cc.tui_native.scroll_view_ import ScrollView  # noqa: E402
from micro_cc.tui_native.stack_ import HSplit, VStack  # noqa: E402
from micro_cc.tui_native.text_utils_ import strip_terminal_sequences  # noqa: E402
from micro_cc.utils import settings_store_  # noqa: E402

W, H = 200, 20
PANE_MOD = """
def register(on):
    @on('pane', name='p')
    def draw(api, e, nxt):
        return [f"PANE {e['width']}x{e['height']} {'F' if e['focused'] else '-'}"]
    @on('pane_click', name='p')
    def click(api, e, nxt):
        CLICKS.append((e['x'], e['y'], e['button'], e['release']))
    @on('pane_key', name='p')
    def key(api, e, nxt):
        KEYS.append(e['data'])
        return True
CLICKS, KEYS = [], []
"""


class Width:
    """Child that shows the width it was rendered at."""
    def render(self, width):
        return [f"row{i} w{width} " + "a" * (width - 12) for i in range(5)]

    def invalidate(self):
        pass


def make_app():
    sv = ScrollView(Width(), follow="end", primary=True)
    split = HSplit(sv)
    root = VStack()
    root.add(split, grow=1)
    prompt = object()
    app = SimpleNamespace(_pane_name=None, _pane_focused=False, _input_mode="idle", main_split=split, root=root,
                          tui=TuiAltScreen(root), prompt=prompt, focus=prompt, flashes=[],
                          request_render=lambda: None)
    app.get_focus = lambda: app.focus
    app._flash_status = lambda text, seconds=0: app.flashes.append(text)
    split.on_close = lambda: pane_.on_closed(app)
    app.tui.set_primary_scroll_view(sv)
    return app, sv


def frame(app, width=W):
    return app.root.render_in(width, H)


class PaneCase(ModsCase):
    def setUp(self):
        super().setUp()
        # settings path is frozen at import (under the real HOME): point it at this test's temp HOME
        self._pg = os.environ.pop("MICRO_CC_POSTGRES_URL", None)
        self._settings = settings_store_._SETTINGS_PATH
        settings_store_._SETTINGS_PATH = Path(self.home) / ".micro-cc" / "settings.json"
        self._w("mods/pm/mod.py", PANE_MOD)
        mods_.load()
        self.mod = sys.modules["micro_cc_mods.pm.mod"]
        self.app, self.sv = make_app()


    def tearDown(self):
        settings_store_._SETTINGS_PATH = self._settings
        if self._pg is not None:
            os.environ["MICRO_CC_POSTGRES_URL"] = self._pg
        super().tearDown()


class TestPersist(PaneCase):
    def test_open_close_saved_and_restored(self):
        pane_.open_pane(self.app, "p", "55%")
        self.assertEqual(settings_store_.get_setting("pane"), {"name": "p", "width": "55%", "side": "right"})
        fresh, _ = make_app()
        pane_.restore(fresh)
        self.assertEqual(fresh._pane_name, "p")
        self.assertEqual(fresh.main_split.pane_width, "55%")
        pane_.close_pane(self.app)
        self.assertEqual(settings_store_.get_setting("pane"), {})
        fresh, _ = make_app()
        pane_.restore(fresh)
        self.assertIsNone(fresh._pane_name)

    def test_restore_skips_missing_mod_silently_and_keeps_pref(self):
        settings_store_.edit_setting("pane", {"name": "gone", "width": "40%"})
        pane_.restore(self.app)
        self.assertIsNone(self.app._pane_name)
        self.assertEqual(self.app.flashes, [])
        self.assertEqual(settings_store_.get_setting("pane")["name"], "gone")

    def test_restore_never_raises(self):
        settings_store_._SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        settings_store_.edit_setting("pane", "not-a-dict")
        pane_.restore(self.app)  # bad value: ignored
        orig = settings_store_.get_setting
        settings_store_.get_setting = lambda k: (_ for _ in ()).throw(OSError("disk"))
        try:
            pane_.restore(self.app)
        finally:
            settings_store_.get_setting = orig
        self.assertIn("pane restore", " ".join(mods_.errors()))

    def test_failure_close_keeps_saved_pane(self):
        pane_.open_pane(self.app, "p")
        self._w("mods/pm/mod.py", "def register(on):\n    @on('pane', name='p')\n"
                "    def draw(api, e, nxt):\n        raise RuntimeError('x')\n")
        mods_.load()
        frame(self.app)
        self.assertIsNone(self.app._pane_name)
        self.assertEqual(settings_store_.get_setting("pane")["name"], "p")


class TestLayout(PaneCase):
    def test_split_render_and_collapse(self):
        lines = frame(self.app)
        self.assertEqual(self.sv.last_width, W)  # closed: full width
        self.assertTrue(pane_.open_pane(self.app, "p"))
        lines = frame(self.app)
        pw = W * 40 // 100
        self.assertEqual(self.app.main_split.rect, (W - pw, 0, pw, H))
        self.assertEqual(self.sv.last_width, W - pw - 1)
        plain = strip_terminal_sequences(lines[0])
        self.assertEqual(plain.index("│"), W - pw - 1)
        self.assertIn(f"PANE {pw}x{H} -", plain)
        frame(self.app, width=70)  # too narrow for MIN_LEFT + MIN_PANE
        self.assertIsNone(self.app.main_split.rect)
        self.assertEqual(self.sv.last_width, 70)

    def test_long_lines_cropped_not_wrapped(self):
        self._w("mods/pm/mod.py", "def register(on):\n    @on('pane', name='p')\n"
                "    def draw(api, e, nxt):\n        return ['a ' * 200, 'second']\n")
        mods_.load()
        lines = mods_.render_pane("p", 40, 10, False)
        self.assertEqual([strip_terminal_sequences(l).strip()[-6:] for l in lines], ["a a a…", "second"])

    def test_kitty_image_survives_beside_pane(self):
        from micro_cc.tui_native.alt_screen_ import extract_kitty_image_ids
        from micro_cc.tui_native.detect_images_ import encode_kitty
        seq = encode_kitty("A" * 9000, 20, 3, 77)  # chunked transmit
        class Img:
            def render(self, width):
                return ["text", seq, "", "", "after"]
            def invalidate(self):
                pass
        self.app.main_split.left = ScrollView(Img(), follow="end", primary=True)
        self.assertTrue(pane_.open_pane(self.app, "p"))
        lines = frame(self.app)
        self.assertEqual(extract_kitty_image_ids(lines), {77})
        self.assertTrue(lines[1].startswith(seq))  # placed at column 0, whole transmit kept
        self.assertIn("│", strip_terminal_sequences(lines[1]))  # pane still stitched on the image row

    def test_unknown_pane_refused(self):
        self.assertFalse(pane_.open_pane(self.app, "nope"))
        self.assertIsNone(self.app.main_split.pane)
        self.assertTrue(self.app.flashes)


class TestVertical(PaneCase):
    """side="top"/"bottom": full-width strip, height from the size spec, same routing and failure rules."""

    def test_bottom_layout_mouse_and_clicks(self):
        self.assertTrue(pane_.open_pane(self.app, "p", "30%", side="bottom"))
        lines = frame(self.app)
        ph = H * 30 // 100
        left_h = H - ph - 1
        split = self.app.main_split
        self.assertEqual(split.rect, (0, left_h + 1, W, ph))
        self.assertEqual((self.sv.last_width, self.sv.viewport_height), (W, left_h))
        self.assertEqual(set(strip_terminal_sequences(lines[left_h])), {"─"})
        self.assertIn(f"PANE {W}x{ph} -", strip_terminal_sequences(lines[left_h + 1]))
        self.assertEqual(self.app.root.find_offset(self.sv), 0)
        self.assertTrue(pane_.route_mouse(self.app, {"x": 3, "y": left_h + 3, "button": 0, "release": False}))
        self.assertEqual(self.mod.CLICKS, [(3, 2, 0, False)])
        self.assertFalse(pane_.route_mouse(self.app, {"x": 3, "y": 2, "button": 0, "release": False}))
        self.assertIs(self.app.root.find_component_at(left_h + 2), split)  # pane rows never hit a message
        self.assertIs(self.app.root.find_component_at(left_h), split)      # nor the divider

    def test_top_shifts_conversation_search_and_selection(self):
        self.assertTrue(pane_.open_pane(self.app, "p", 5, side="top"))
        screen = frame(self.app)
        self.assertEqual(self.app.main_split.rect, (0, 0, W, 5))
        self.assertIn("PANE", strip_terminal_sequences(screen[0]))
        self.assertEqual(self.app.root.find_offset(self.sv), 6)
        self.assertTrue(strip_terminal_sequences(screen[6]).startswith("row0"))
        self.assertIsInstance(self.app.root.find_component_at(6), Width)
        tui = self.app.tui
        tui.set_search_query("row1 ")
        tui._refresh_search_matches(W)
        lit = tui.apply_search_highlights(screen)
        self.assertEqual([i for i, l in enumerate(lit) if l != screen[i]], [7])
        self.assertEqual(tui._scroll_region()[1], 6)

    def test_too_short_collapses_and_reopens(self):
        pane_.open_pane(self.app, "p", "50%", side="bottom")
        lines = self.app.root.render_in(W, 8)  # 8 rows can't fit MIN_ROWS + divider + MIN_PANE_ROWS
        self.assertIsNone(self.app.main_split.rect)
        self.assertEqual(len(lines), 8)
        self.assertEqual(self.sv.viewport_height, 8)
        frame(self.app)
        self.assertIsNotNone(self.app.main_split.rect)

    def test_side_persisted_and_bad_side_refused(self):
        pane_.open_pane(self.app, "p", "35%", side="top")
        fresh, _ = make_app()
        pane_.restore(fresh)
        self.assertEqual((fresh._pane_name, fresh.main_split.side), ("p", "top"))
        self.assertFalse(pane_.open_pane(self.app, "p", side="left"))
        self.assertEqual(settings_store_.get_setting("pane")["side"], "top")

    def test_switching_side_resets_left_offset(self):
        pane_.open_pane(self.app, "p", 5, side="top")
        frame(self.app)
        pane_.open_pane(self.app, "p", "40%")
        frame(self.app)
        self.assertEqual(self.app.root.find_offset(self.sv), 0)
        self.assertEqual(self.app.main_split.rect, (W - W * 40 // 100, 0, W * 40 // 100, H))


class TestFailure(PaneCase):
    def setUp(self):
        super().setUp()
        self._hang = (mods_.HANG_S, mods_.SLOW_S)
        mods_.HANG_S, mods_.SLOW_S = 0.05, 10

    def tearDown(self):
        mods_.HANG_S, mods_.SLOW_S = self._hang
        super().tearDown()

    def test_hung_pane_closes_and_conversation_reflows(self):
        self._w("mods/pm/mod.py", "import time\ndef register(on):\n    @on('pane', name='p')\n"
                "    def draw(api, e, nxt):\n        time.sleep(5)\n")
        mods_.load()
        pane_.open_pane(self.app, "p")
        self.app._pane_focused = True
        frame(self.app)  # watchdog interrupts, mod disabled, pane reports closed
        self.assertIn("hung", " ".join(mods_.errors()))
        self.assertIsNone(self.app._pane_name)
        self.assertFalse(self.app._pane_focused)
        self.assertIsNone(self.app.main_split.pane)
        frame(self.app)
        self.assertEqual(self.sv.last_width, W)

    def test_runtime_disable_is_logged_and_notified(self):
        seen = []
        mods_.on_fail(seen.append)
        try:
            self._w("mods/pm/mod.py", "def register(on):\n    @on('pane', name='p')\n"
                    "    def draw(api, e, nxt):\n        raise RuntimeError('boom')\n")
            mods_.load()
            pane_.open_pane(self.app, "p")
            frame(self.app)
        finally:
            mods_.on_fail(None)
        self.assertTrue(seen and "mod pm disabled" in seen[0] and "boom" in seen[0])
        self.assertIn("mods/pm", (Path(self.home) / ".micro-cc" / "mods.log").read_text())

    def test_failing_bottom_pane_closes_and_reflows_height(self):
        self._w("mods/pm/mod.py", "def register(on):\n    @on('pane', name='p')\n"
                "    def draw(api, e, nxt):\n        raise RuntimeError('boom')\n")
        mods_.load()
        pane_.open_pane(self.app, "p", "40%", side="bottom")
        lines = frame(self.app)
        self.assertIsNone(self.app._pane_name)
        self.assertIsNone(self.app.main_split.rect)
        self.assertEqual((len(lines), self.sv.viewport_height), (H, H))

    def test_raising_pane_closes(self):
        self._w("mods/pm/mod.py", "def register(on):\n    @on('pane', name='p')\n"
                "    def draw(api, e, nxt):\n        raise RuntimeError('x')\n")
        mods_.load()
        pane_.open_pane(self.app, "p")
        frame(self.app)
        self.assertIsNone(self.app._pane_name)

    def test_invalid_markup_shown_not_fatal(self):
        self._w("mods/pm/mod.py", "def register(on):\n    @on('pane', name='p')\n"
                "    def draw(api, e, nxt):\n        return ['[/nope]']\n")
        mods_.load()
        pane_.open_pane(self.app, "p")
        lines = frame(self.app)
        self.assertIn("invalid markup", strip_terminal_sequences(lines[0]))
        self.assertEqual(self.app._pane_name, "p")


class TestRouting(PaneCase):
    def test_mouse_in_pane_goes_to_mod_not_selection(self):
        pane_.open_pane(self.app, "p")
        frame(self.app)
        col = self.app.main_split.rect[0]
        self.assertTrue(pane_.route_mouse(self.app, {"x": col + 3, "y": 2, "button": 0, "release": False}))
        self.assertEqual(self.mod.CLICKS, [(3, 2, 0, False)])
        self.assertTrue(self.app._pane_focused)
        self.assertFalse(self.app.tui.selection_press_active)
        self.assertFalse(pane_.route_mouse(self.app, {"x": 5, "y": 2, "button": 0, "release": False}))

    def test_drag_select_keeps_mouse_when_crossing_into_pane(self):
        pane_.open_pane(self.app, "p")
        frame(self.app)
        self.app.tui.selection_press_active = True
        col = self.app.main_split.rect[0]
        self.assertFalse(pane_.route_mouse(self.app, {"x": col + 3, "y": 2, "button": 32, "release": False}))

    def test_keys_focus_escape_and_picker(self):
        pane_.open_pane(self.app, "p")
        frame(self.app)
        self.assertFalse(pane_.route_key(self.app, "a", "a"))  # unfocused: prompt gets it
        self.assertTrue(pane_.route_key(self.app, "\x1d", "ctrl+]"))
        self.assertTrue(self.app._pane_focused)
        self.assertTrue(pane_.route_key(self.app, "j", "j"))
        self.assertEqual(self.mod.KEYS, ["j"])
        self.assertFalse(pane_.route_key(self.app, "\x03", "ctrl+c"))
        self.assertTrue(pane_.route_key(self.app, "\x1b", "escape"))
        self.assertFalse(self.app._pane_focused)
        pane_.focus(self.app)
        self.app._input_mode = "query_active"  # mid-turn: Esc leaves the pane and still reaches the interrupt
        self.assertFalse(pane_.route_key(self.app, "\x1b", "escape"))
        self.assertFalse(self.app._pane_focused)
        self.app._input_mode = "idle"
        pane_.focus(self.app)
        self.app.focus = object()  # a question panel took the prompt slot
        self.assertFalse(pane_.route_key(self.app, "k", "k"))
        self.assertFalse(self.app._pane_focused)

    def test_resize_collapse_drops_focus(self):
        pane_.open_pane(self.app, "p")
        frame(self.app)
        pane_.focus(self.app)
        frame(self.app, width=70)
        self.assertFalse(pane_.route_key(self.app, "a", "a"))
        self.assertFalse(self.app._pane_focused)

    def test_subagent_swap_keeps_pane(self):
        pane_.open_pane(self.app, "p")
        other = ScrollView(Width(), follow="end")
        self.app.main_split.left = other
        frame(self.app)
        self.assertEqual(self.app.root.find_offset(other), 0)
        self.assertIsNotNone(self.app.main_split.rect)
        self.assertEqual(other.last_width, W - W * 40 // 100 - 1)


class TestSearchSelection(PaneCase):
    def test_search_and_selection_use_left_width(self):
        pane_.open_pane(self.app, "p")
        screen = frame(self.app)
        lw = self.sv.last_width
        self.app.tui.set_search_query(f"w{lw}")
        self.app.tui._refresh_search_matches(W)
        self.assertEqual(len(self.app.tui.search_matches), 5)
        tui = self.app.tui
        tui.selection_in_content = True
        tui.selection_anchor, tui.selection_focus = SelectionPoint(0, 0), SelectionPoint(3, 4)
        out = tui.apply_selection(screen)
        mid = out[1]
        self.assertNotIn("\x1b[7m", mid[mid.index("│"):])  # highlight stops before the pane
        region = tui._scroll_region()
        self.assertEqual(tui._content_point(region, SelectionPoint(2, W - 1)).col, lw - 1)


class TestAgentObserve(ModsCase):
    def test_observe_isolates_and_filters(self):
        self._w("mods/a/mod.py", "SEEN = []\ndef register(on):\n    @on('agent')\n"
                "    def boom(api, e):\n        raise RuntimeError('x')\n")
        self._w("mods/b/mod.py", "SEEN = []\ndef register(on):\n    @on('agent')\n"
                "    def see(api, e):\n        e['mutated'] = 1\n        SEEN.append(e['type'])\n")
        mods_.load()
        seen = sys.modules["micro_cc_mods.b.mod"].SEEN
        event = {"type": "tool_call", "name": "bash_", "id": "1"}
        mods_.observe(event)
        mods_.observe({"type": "text_delta", "content": "x"})
        self.assertEqual(seen, ["tool_call"])
        self.assertNotIn("mutated", event)
        self.assertIn("mods/a", " ".join(mods_.errors()))
        mods_.observe({"type": "done"})
        self.assertEqual(seen, ["tool_call", "done"])

    def test_registration_checks(self):
        self._w("mods/f/mod.py", "def register(on):\n    on('agent', type='x')(lambda a, e: None)\n")
        self._w("mods/g/mod.py", "def register(on):\n    on('pane')(lambda a, e, n: None)\n")
        self.assertEqual(mods_.load(), [])


if __name__ == "__main__":
    unittest.main()
