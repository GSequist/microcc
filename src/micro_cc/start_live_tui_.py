import asyncio
import traceback
import os
import re
import subprocess
import sys
import platform

from rich.markup import escape as _markup_escape
from micro_cc.utils import self_reload_
from micro_cc.utils.history_mount_ import history_mount_
from micro_cc.utils.msg_normalize_ import cut_short_calls
from micro_cc.utils.msg_store_ import load_msgs, rewind_msgs, store_msgs
from micro_cc.utils.session_ipc_ import start_listener, stop_listener
from micro_cc.utils.helpers import (
    check_ollama,
    ollama_daemon_up,
    start_ollama_daemon,
    extract_image_paths,
    sanitize_and_encode_image_,
)
from micro_cc.cache import state_store
from micro_cc.utils import banner_
from micro_cc.utils import theme_store_
from micro_cc.tui_native.theme_ import shimmer
from micro_cc.tui_native.glyphs_ import glyph

# Theme tokens read live via theme_store_.get() to allow /theme switch.
from micro_cc.tui_native.screen_cmds_ import SLASH_COMMANDS, PREFIX_COMMANDS
from micro_cc import mods_
from micro_cc.utils import command_registry, statusline_
from micro_cc.tui_native.ui_api_ import make_ui
from micro_cc.tui_native import glyphs_, login_ui_, pollers_, self_reload_ui_, status_bar_, question_ui_, panel_layout_
from micro_cc.tui_native.self_update_ import check_and_update
from micro_cc.tui_native.self_terminal_setup_ import _background_terminal_setup_check
from micro_cc.tui_native.etype_handler_ import ETYPES
from micro_cc.utils.project_files_ import list_project_files, rank_file_matches
from micro_cc.models import catalog as model_catalog
from micro_cc.models.providers import active_provider, any_configured
from micro_cc.utils import settings_store_, whats_new
from micro_cc.utils.tokenization_simple import load_token_stats
from micro_cc.claude_loop_ import claude_loop
from collections import deque
from micro_cc.tui_native.alt_screen_ import TuiAltScreen, ENTER_ALT_SCREEN, EXIT_ALT_SCREEN
from micro_cc.tui_native.stack_ import HSplit, VStack
from micro_cc.tui_native import pane_
from micro_cc.tui_native.scroll_view_ import ScrollView
from micro_cc.tui_native.message_row_ import MessageRow, StreamingRow, RichStatic, HRule
from micro_cc.tui_native.prompt_input_ import PromptInput
from micro_cc.tui_native.terminal_ import ProcessTerminal
from micro_cc.tui_native.stdin_buffer_ import StdinBuffer, Paste
from micro_cc.tui_native.keys_ import parse_key, is_key_release
from micro_cc.utils.msg_store_ import load_checkpoint, load_memory_review_recap
from micro_cc.tui_native.option_panel_ import OptionPanel, PanelItem

# --------------- background noise: discard, don't persist ---------------
# Background-thread logging to stderr corrupts the alt-screen; swallow it.
import logging as _logging



_logging.getLogger().addHandler(_logging.NullHandler())
_logging.getLogger().setLevel(_logging.WARNING)

import builtins as _builtins

_real_print = _builtins.print


def _bg_print(*args, **kwargs):
    pass


_builtins.print = _bg_print

# --------------- sleep inhibitor ---------------
# macOS only via caffeinate -i; no-op on Linux/Docker.


def _inhibit_sleep():
    """Start system idle sleep inhibitor."""
    if platform.system() != "Darwin":
        return None
    return subprocess.Popen(
        ["caffeinate", "-i"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )


def _allow_sleep(handle):
    """Release the sleep inhibition."""
    if handle is None:
        return
    handle.terminate()
    handle.wait()




# Render loop heartbeat ~30ms; dirty flag marks what repaints.

class MicroTui():

    _current_model = settings_store_.get_setting("model")
    _trim_budget = model_catalog.effective_trim_budget(_current_model)

    ALLOW_SELECT = True

    _ESC_CLEAR_TIMEOUT = 1.5
    _SPINNER = glyph("spinner")
    _RENDER_FRAME = 0.03  # cadence do_render is called at while the app is running
    _STATIC_HINT_LINE_MAXLEN = 200
    _STATUS_LINE_TIMEOUT = 0.3
    _STATUS_LINE_MAXLEN = 200

    _update_available = None
    _STATIC_HINT_LINE_MAXLEN = 200

    def __init__(self, project_dir: str, messages: list):
        self._project_dir = project_dir
        load_token_stats(project_dir)
        self.__existing_msgs = messages
        # Backs the @ mention picker; loaded once at mount.
        self._project_files = []
        self._session_server = None  # Unix socket for message_session_, bound in start()
        self._loop_msgs = None  # entire loop_msgs owned
        self._msg_rows: list[dict] = []  # ordered list of msg dicts (for /copy + cancel)
        # state machine
        self._input_mode = "idle"  # "idle" | "login" | "keys" | "awaiting_approval" | "question_asked" | "query_active" | "gui"
        # Set while /gui has handed the session to the browser.
        self._gui_url = None
        self._gui_shutdown = None
        ## user queries queueing
        self._queue = deque()
        ## "working on…" nudge (above the input) while a query is active
        self._working_timer = None
        self._spin_i = 0
        # _login_stage: None or the env-var name being collected; _login_values: {name: value}
        self._login_stage = None
        self._login_provider = None
        self._login_values = {}
        # /keys: _keys_stage is None | "name" | "value"; values accumulate until a blank name.
        self._keys_stage = None
        self._keys_pending_name = None
        self._keys_values = {}
        ## ask user
        self._ask_questions = None
        self._ask_stage = None
        self._ask_answers = {}
        ## Esc-Esc-to-clear (only while idle — see action_cancel_query)
        self._esc_clear_armed = False
        self._esc_clear_timer_task: asyncio.Task | None = None
        ## subagent orchestration. Populated from disk, refreshed every tick.
        self._tracked_subagents = []
        self._subagent_poll_cache = {}  # project_dir -> (mtime_ns, size) from last poll
        self._subagent_poll_timer = None
        self._subagent_viewing_target: str | None = None
        self._subagent_snapshot: list[dict] = []  # last poll, read by mods via api.subagents()
        ## mod side pane (tui_native/pane_.py)
        self._pane_name: str | None = None
        self._pane_focused = False
        # Seed from disk so an old compaction doesn't flash "compacted just now" on launch.
        _existing_checkpoint = load_checkpoint(self._project_dir)
        self._last_checkpoint_index = (
            _existing_checkpoint["as_of_index"] if _existing_checkpoint is not None else 0
        )
        ## background bash processes; polled from start().
        self._bgproc_poll_timer = None
        # True while the bgproc picker holds the prompt slot; independent of _input_mode.
        self._bgproc_viewing = False
        # Seed from disk so an old memory review doesn't flash on first poll.
        _existing_review = load_memory_review_recap(self._project_dir)
        self._last_memory_review_ts = _existing_review["ts"] if _existing_review is not None else None
        self._memory_review_poll_timer = None

        ## scheduled prompts; the timer is created lazily by _ensure_schedule_tick.
        self._schedule_poll_timer: asyncio.Task | None = None

        self._pending_input: asyncio.Event | None = None
        self._pending_result = None
        self._streaming: StreamingRow | None = None
        # Tracked explicitly: nothing else cancels the do_query task, so ESC wouldn't stop the stream.
        self._current_query_task: asyncio.Task | None = None
        self._update_available = None
        self._whats_new_row = None
        self._flash_status_timer_task: asyncio.Task | None = None
        self._memory_flash_timer_task: asyncio.Task | None = None

        ## restart-on-self-change: the poll flags changes; reload_harness_ applies them at a turn boundary.
        self._reload_pending = False
        self._reload_changed: set = set()
        self._self_poll_timer: asyncio.Task | None = None
        self._restarting = False  # guards against a second restart mid-relaunch

        # --- render loop bookkeeping ---
        self._dirty = True
        self._running = False
        self._render_task: asyncio.Task | None = None
        self._last_render_crash: str | None = None  # see _render_loop's except branch
        self._focused = None  # set_focus/get_focus — see _on_terminal_input for real dispatch

        self.prompt = None

        # --- input loop ---
        self.terminal = ProcessTerminal()
        self._stdin_buffer = StdinBuffer()
        self._stdin_reader_registered = False

        # Mods load once, before the widget tree renders; legacy seams are swept first.
        self._legacy_moved = mods_.sweep_legacy()
        mods_.bind(self._get_ui)
        mods_.on_fail(lambda text: self._flash_status(_markup_escape(text), seconds=8))
        mods_.load()
        glyphs_.reload()

        self._build_widget_tree()
        pane_.restore(self)

    # --- widget tree -----------------------------------------------------
    def _build_widget_tree(self) -> None:
        # Keep the hue so the hint bar can match the wordmark.
        _banner_markup, self._banner_hue = banner_.banner()
        # name= makes a component a mod render target; header/above_input are empty anchors for mods.
        self.banner = RichStatic(_banner_markup, name="banner")
        self.header_panels = RichStatic("", max_lines=12, name="header")
        self.above_input_panels = RichStatic("", max_lines=12, name="above_input")

        self.prompt = PromptInput()
        # on_submit is sync; wrap the async handler in a task.
        self.prompt.on_submit = lambda text: self._safe_task(self._on_prompt_submitted(text), "prompt submit")
        self.prompt.on_change = self._on_prompt_changed
        self.message_list = VStack()
        self.messages_scroll = ScrollView(self.message_list, follow="end", primary=True)
        # Separate VStack so the subagent view doesn't share content with message_list.
        self.subagent_message_list = VStack()
        self.subagent_scroll_view = ScrollView(
            self.subagent_message_list, follow="end", primary=False
        )

        self.working = RichStatic("", single_line=True, name="working")
        self.ask_question_text = RichStatic("", name="question")  # genuinely multi-line — full question text wraps
        self.statusbar = RichStatic("", single_line=True, name="statusbar")
        # Own timer: a memory review can overlap the compaction flash, which shares one slot.
        self.memory_flash = RichStatic("", max_lines=5, name="memory_flash")
        self.static_hintbar = RichStatic("", single_line=True, name="hintbar")
        # Not single_line: one row per tracked item.
        self.subagent_status = RichStatic("", name="subagents")
        self.bgproc_status = RichStatic("", name="bgprocs")
        self.top_rule = HRule()
        self.bottom_rule = HRule()

        self.bottom_bar = VStack()
        self.bottom_bar.add(self.working)
        self.bottom_bar.add(self.memory_flash)
        self.bottom_bar.add(self.above_input_panels)
        # Live filter for / and @: always mounted, never takes focus.
        self._live_panel = OptionPanel(show=lambda _p: self.request_render(), hide=self.request_render)
        self.bottom_bar.add(self._live_panel)
        self.bottom_bar.add(self.top_rule)
        self.bottom_bar.add(self.prompt)
        self.bottom_bar.add(self.bottom_rule)
        self.bottom_bar.add(self.statusbar)
        self.bottom_bar.add(self.static_hintbar)
        self.bottom_bar.add(self.subagent_status)
        self.bottom_bar.add(self.bgproc_status)
        # Component currently in the prompt's slot (swapped for ask_user pickers).
        self._active_prompt_slot_component = self.prompt

        self.root = VStack()
        # shrink=0 keeps banner/bottom_bar from being squeezed by a tall conversation.
        self.root.add(self.banner, basis="auto", shrink=0)
        self.root.add(self.header_panels, basis="auto", shrink=0)
        # Conversation (or a subagent's) on the left, optional mod pane on the right.
        self.main_split = HSplit(self.messages_scroll)
        self.main_split.on_close = lambda: pane_.on_closed(self)
        self.root.add(self.main_split, grow=1)
        self.root.add(self.bottom_bar, basis="auto", shrink=0)

        self.tui = TuiAltScreen(self.root)
        self.tui.set_primary_scroll_view(self.messages_scroll)

        # Modal panel: replaces the prompt in its slot (model, login, theme, rewind, ask-user, approval, ...).
        self._option_panel = OptionPanel(
            show=self._show_modal_panel,
            hide=lambda: self._swap_into_prompt_slot(self.prompt),
        )

    # --- render loop / focus ---------------------------------------------

    def request_render(self) -> None:
        self._dirty = True

    async def _render_loop(self) -> None:
        while self._running:
            if self.tui.autoscroll_tick():
                self._dirty = True
            if self._dirty:
                try:
                    self.tui.do_render()
                except Exception as e:
                    # Render errors (e.g. MarkupError from unescaped "[") would end this loop and freeze the screen.
                    self._dirty = False
                    # _show_error_row re-dirties; report each distinct failure once to avoid a crash loop.
                    key = f"{type(e).__name__}: {e}"
                    if key != self._last_render_crash:
                        self._last_render_crash = key
                        tb = traceback.format_exc()
                        location = ""
                        pkg_dir = os.path.dirname(os.path.abspath(__file__))
                        for frame in reversed(traceback.extract_tb(e.__traceback__)):
                            if frame.filename.startswith(pkg_dir):
                                location = f" ({os.path.basename(frame.filename)}:{frame.lineno})"
                                break
                        try:
                            log_path = os.path.expanduser("~/.micro-cc/last_render_crash.log")
                            os.makedirs(os.path.dirname(log_path), exist_ok=True)
                            with open(log_path, "w") as f:
                                f.write(tb)
                        except OSError:
                            pass
                        try:
                            self._show_error_row(
                                f"render crashed: {type(e).__name__}: {_markup_escape(str(e))}"
                                f"{_markup_escape(location)} — full traceback: ~/.micro-cc/last_render_crash.log"
                            )
                        except Exception:
                            pass
                else:
                    self._dirty = False
            await asyncio.sleep(self._RENDER_FRAME)

    def _call_later(self, delay: float, fn) -> asyncio.Task:
        async def _wait():
            await asyncio.sleep(delay)
            fn()
        return asyncio.create_task(_wait())

    def _show_error_row(self, text: str) -> None:
        """Append an error row to the transcript instead of logging to disk."""
        err = {"type": "error", "content": text}
        self._msg_rows.append(err)
        row = MessageRow(err)
        self.message_list.add(row)
        self._scroll_to_bottom()
        self.request_render()

    def _safe_task(self, coro, label: str = "") -> asyncio.Task:
        """Wrap task to catch exceptions and show as error row instead of corrupting screen."""
        async def _wrapped():
            try:
                await coro
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self._show_error_row(f"{label or 'background task'} crashed: {type(e).__name__}: {e}")
        return asyncio.create_task(_wrapped())

    def set_focus(self, component) -> None:
        # _on_terminal_input routes keys via self._focused.
        if self._focused is not None and hasattr(self._focused, "focused"):
            self._focused.focused = False
        self._focused = component
        if component is not None and hasattr(component, "focused"):
            component.focused = True
        self.request_render()

    def get_focus(self):
        return self._focused

    # --- input loop ---
    # asyncio.add_reader for non-blocking stdin.

    def _on_stdin_readable(self) -> None:
        try:
            data = os.read(sys.stdin.fileno(), 4096)
        except OSError:
            return
        if not data:
            return
        try:
            self._stdin_buffer.feed(data)
            for item in self._stdin_buffer.complete_sequences():
                self._handle_input_item(item)
        except Exception as e:
            self._show_error_row(f"input handler crashed: {type(e).__name__}: {e}")

    def _handle_input_item(self, item) -> None:
        if isinstance(item, Paste):
            self.on_paste(item.text)
            self.request_render()
            return
        # feed_stdin handles Kitty negotiation and calls _on_terminal_input itself.
        self.terminal.feed_stdin(item)

    def _on_terminal_input(self, data: str) -> None:
        # Filter key-release events from Kitty keyboard protocol to avoid double dispatch.
        if is_key_release(data):
            return
        mouse = self.tui.parse_sgr_mouse_event(data)
        if mouse is not None:
            if pane_.route_mouse(self, mouse):
                return
            # Intercept wheel events (button 64/65) before selection state machine.
            if mouse["button"] in (64, 65):
                if not mouse["release"]:
                    if mouse["button"] == 64:
                        self.action_scroll_messages_up()
                    else:
                        self.action_scroll_messages_down()
                return
            hit = self.tui.overlay_hit(mouse["x"], mouse["y"])
            if hit is not None:
                # Tap on a picker item selects it; press and drag-select are ignored over overlays.
                click_row = getattr(hit[0], "click_row", None)
                if mouse["release"] and click_row is not None and click_row(hit[1]):
                    self.request_render()
                return
            self.tui.handle_selection_mouse_event(mouse)
            # Click-to-expand: toggle expandable component on release without drag.
            if mouse["release"] and self.tui.get_selection_bounds() is None:
                component = self.root.find_component_at(mouse["y"])
                click_row = getattr(component, "click_row", None)
                offset = self.root.find_offset(component) if click_row is not None else None
                if offset is not None and click_row(mouse["y"] - offset):
                    self.request_render()
                    return
                if component is not None and hasattr(component, "toggle_expanded"):
                    if component.toggle_expanded():
                        self.request_render()
            self.request_render()
            return
        key_id = parse_key(data)
        if key_id is None:
            return
        if pane_.route_key(self, data, key_id):
            self.request_render()
            return
        if (
            self._input_mode in ("idle", "query_active")
            and self.get_focus() is self.prompt
            and not self._live_panel.is_open
        ):
            if mods_.handle_key(data):
                self.request_render()
                return
        if key_id == "escape":
            # App-level so it fires regardless of focus; action_cancel_query also closes pickers.
            self.action_cancel_query()
            self.request_render()
            return

        _SCROLL_KEYS = {
            "ctrl+up": self.action_scroll_messages_up,
            "ctrl+down": self.action_scroll_messages_down,
            "ctrl+pageup": self.action_scroll_messages_page_up,
            "ctrl+pagedown": self.action_scroll_messages_page_down,
            "ctrl+home": self.action_scroll_messages_top,
            "ctrl+end": self.action_scroll_messages_bottom,
        }
        if key_id in _SCROLL_KEYS:
            _SCROLL_KEYS[key_id]()
            return

        # Live filter panels (slash/at) stay unfocused; intercept their navigation keys.
        live_panel = self._live_panel if self._live_panel.is_open else None

        if (
            key_id == "shift+tab"
            and self.get_focus() is self.prompt
            and live_panel is None
        ):
            if not self._tracked_subagents:
                self._flash_status("no subagents to switch to", seconds=2.5)
                return
            cycle = [None, *self._tracked_subagents]
            try:
                current_idx = cycle.index(self._subagent_viewing_target)
            except ValueError:
                # Viewed subagent was pruned; fall back to boss.
                current_idx = 0
            next_target = cycle[(current_idx + 1) % len(cycle)]
            if next_target is None:
                self.exit_subagent_view()
            else:
                self.enter_subagent_view(next_target)
            return

        # Separate branch: once open, focus is on the picker, not the prompt.
        if key_id == "ctrl+b" and self._bgproc_viewing:
            self._hide_bgproc_detail()
            return
        if (
            key_id == "ctrl+b"
            and self.get_focus() is self.prompt
            and live_panel is None
        ):
            self._show_bgproc_detail()
            return

        if live_panel is not None:
            if key_id in ("up", "down"):
                live_panel.handle_key(key_id)
                self.request_render()
                return
            if key_id == "enter":
                live_panel.confirm()
                self.request_render()
                return
            # Tab falls through to the prompt: it completes text without submitting.

        focused = self.get_focus()
        if focused is not None and hasattr(focused, "handle_key"):
            if focused.handle_key(key_id):
                # Cursor-only keys never call _changed(), so they need an explicit repaint.
                self.request_render()
                # Re-derive the hint only for newline keys; they flip the used_multiline flag.
                if key_id in ("shift+enter", "alt+enter", "ctrl+j", "enter"):
                    self._static_hint_text()

    def _on_terminal_resize(self) -> None:
        # SIGWINCH handler; signal-context safe.
        self.request_render()

    # --- pickers ---

    def _on_model_picked(self, item: PanelItem) -> None:
        self._current_model = item.value
        settings_store_.edit_setting("model", item.value)
        if item.choices:
            settings_store_.edit_setting("reasoning_effort", item.chosen)
        self._trim_budget = model_catalog.effective_trim_budget(self._current_model)
        self._refresh_status()

    def _on_theme_picked(self, item: PanelItem) -> None:
        """Apply and persist selected theme preset."""
        msg = theme_store_.set_preset(item.value)
        self._flash_status(msg, seconds=3)

    def _on_subagents_picked(self, item: PanelItem) -> None:
        """Persist concurrent subagent cap to settings."""
        settings_store_.edit_setting("max_subagents", int(item.value))
        self._flash_status(f"✓ up to {item.value} subagent(s) at once — applies to the next spawn", seconds=3)

    def _apply_theme(self, _name: str = "") -> None:
        """Repaint UI when color set changes; re-emit terminal defaults and invalidate caches."""
        from micro_cc.tui_native.alt_screen_ import default_colors_sequence

        sys.stdout.write(default_colors_sequence())
        sys.stdout.flush()

        # Rebuild banner from theme-derived string, not just invalidate.
        banner_markup, hue = banner_.banner()
        self._banner_hue = hue
        self.banner.update(banner_markup)
        self.banner.invalidate()
        for entry in self.message_list.entries:
            entry.component.invalidate()
        for entry in self.subagent_message_list.entries:
            entry.component.invalidate()
        for widget in (
            self.working, self.ask_question_text, self.memory_flash,
            self.static_hintbar, self.statusbar, self.subagent_status,
            self.bgproc_status, self.prompt,
        ):
            widget.invalidate()
        self.tui.invalidate_overlays()
        self._static_hint_text()
        self.request_render()

    def _on_rewind_picked(self, item: PanelItem) -> None:
        self._safe_task(self._do_rewind(int(item.value)), "rewind")

    async def _do_rewind(self, cut_idx: int) -> None:
        all_msgs = load_msgs(self._project_dir)
        rewound_text = all_msgs[cut_idx].get("content", "")
        if not isinstance(rewound_text, str):
            rewound_text = ""
        rewind_msgs(self._project_dir, cut_idx)
        await self._reload_messages_from_disk()
        self.prompt.clear()
        self.prompt.insert(rewound_text)
        self.set_focus(self.prompt)
        self._refresh_status()

    def _ungate_tool(self, name: str) -> None:
        """'Yes, always' on an approval; claude_loop reads settings live, so it applies this turn."""
        settings_store_.set_gated(settings_store_.gated_tools() - {name})
        self._refresh_status()

    def _on_slash_picked(self, item: PanelItem) -> None:
        self.prompt.clear()
        self.prompt.insert(item.value)
        self._safe_task(self._on_prompt_submitted(self.prompt.text), "prompt submit")


    # ============================================= pickers

    # ============================================= logins


    # ============================================= other

    async def _mount_row(self, msg: dict) -> MessageRow:
        row = MessageRow(msg)
        self._msg_rows.append(msg)
        self.message_list.add(row)
        self.request_render()
        # User messages snap to bottom; others respect follow state.
        self._scroll_to_bottom(force=msg.get("type") == "user")
        return row

    def _scroll_to_bottom(self, force: bool = False):
        """Auto-scroll to end, optionally forced."""
        if force:
            self.messages_scroll.scroll_to_end()
        self.request_render()

    def _finalize_streaming(self) -> None:
        if self._streaming is not None:
            content = self._streaming.get_content()
            mode = self._streaming._mode
            self.message_list.remove(self._streaming)
            self._streaming = None
            if content.strip():
                msg = {"type": mode, "content": content}
                row = MessageRow(msg)
                self._msg_rows.append(msg)
                self.message_list.add(row)
            self.request_render()

    # ============================================= other

    # ============================================= modal pane

    def _show_modal_panel(self, panel) -> None:
        self._live_panel.close()  # the prompt is going away; its / and @ panel must not keep the keys
        self._swap_into_prompt_slot(panel)

    def _swap_into_prompt_slot(self, component) -> None:
        """Replace prompt with component in bottom_bar."""
        if component is not self._active_prompt_slot_component:
            self.bottom_bar.replace(self._active_prompt_slot_component, component)
            self._active_prompt_slot_component = component
        self.set_focus(component)
        self.request_render()

    # ============================================= modal pane

    # ============================================= clipboard + paste monkey patching + double paste unravel full paste # TODO

    def on_paste(self, text: str) -> None:
        # Always route paste to prompt, stealing focus if needed.
        if self.get_focus() is not self.prompt:
            self.set_focus(self.prompt)
        self.prompt._handle_paste(text)

    # ============================================= clipboard + paste monkey patching + double paste unravel full paste # TODO

    # ============================================= statusbar

    _STATIC_HINT_LINE_MAXLEN = 200
    _PROJECT_DIR_DISPLAY_MAXLEN = 20  # keep tail readable, not head

    @staticmethod
    def _shorten_path(path: str, maxlen: int) -> str:
        """Keep tail (repo/subdir) readable, not head."""
        if len(path) <= maxlen:
            return path
        return "…" + path[-(maxlen - 1):]

    # Multiline status widgets (bgproc/subagent) capped to prevent overflow.
    _STATUS_STACK_MAX_LINES = 4

    @classmethod
    def _cap_lines(cls, lines: list[str]) -> list[str]:
        if len(lines) <= cls._STATUS_STACK_MAX_LINES:
            return lines
        shown = lines[: cls._STATUS_STACK_MAX_LINES - 1]
        return shown + [f"… +{len(lines) - len(shown)} more"]

    def _static_hint_text(self) -> None:
        return status_bar_.static_hint_text(self)

    # Status script output is used verbatim; reads live data from stdin, disk elsewhere.
    _STATUS_LINE_TIMEOUT = 0.3  # must be tight, blocks event loop
    _STATUS_LINE_MAXLEN = 200  # a runaway script can't blow up the bar

    def _refresh_status(self):
        return status_bar_.refresh_status(self)

    def _flash_status(self, text: str, seconds: float = 3.0):
        return status_bar_.flash_status(self, text, seconds)

    def _disarm_esc_clear(self) -> None:
        """Timeout callback to disarm Esc-Esc-clear after initial Esc."""
        self._esc_clear_armed = False

    # ---------------------------------------------------- restart on self-change

    def _flash_memory_review(self, text: str, seconds: float = 6.0) -> None:
        """Flash memory review text with separate timer to avoid collision with status flashes."""
        self.memory_flash.update(text)
        self.request_render()
        if self._memory_flash_timer_task is not None:
            self._memory_flash_timer_task.cancel()
        self._memory_flash_timer_task = self._call_later(seconds, self._clear_memory_flash)

    def _clear_memory_flash(self) -> None:
        self.memory_flash.update("")
        self.request_render()

    def copy_to_clipboard(self, text: str) -> None:
        """Copy text via pyperclip or OSC 52 fallback."""
        landed = False
        pyperclip_error = None
        try:
            import pyperclip
            pyperclip.copy(text)
            landed = True
        except Exception as e:
            pyperclip_error = f"{type(e).__name__}: {e}"
        if not landed:
            try:
                import base64
                sys.stdout.write(f"\x1b]52;c;{base64.b64encode(text.encode()).decode()}\x07")
                sys.stdout.flush()
            except OSError:
                pass
        if not text:
            return
        if landed:
            self._flash_status(f"✓ copied {len(text)} chars")
        else:
            self._flash_status(f"[{theme_store_.get('error')}]✗ pyperclip failed ({pyperclip_error}) — tried terminal clipboard, enable clipboard access if it didn't land[/{theme_store_.get('error')}]", seconds=5)

    # ============================================= statusbar

    # ============================================= terminal busy and status of terminal logic

    def _term_busy(self, on: bool):
        """Signal working state to terminal via OSC 9;4 progress."""
        seq = "\033]9;4;3;0\a" if on else "\033]9;4;0;0\a"
        try:
            with open("/dev/tty", "w") as tty:
                tty.write(seq)
                tty.flush()
        except OSError:
            pass

    def _alert_attention(self):
        """Ring terminal bell for out-of-band attention signal."""
        try:
            with open("/dev/tty", "w") as tty:
                tty.write("\a")
                tty.flush()
        except OSError:
            pass

    def _set_busy(self, on: bool):
        """Toggle working state: terminal signal and in-app spinner."""
        self._term_busy(on)
        if on:
            if self._working_timer is None:
                self._working_timer = asyncio.create_task(self._working_timer_loop())
            self._tick_working()
        else:
            if self._working_timer is not None:
                self._working_timer.cancel()
                self._working_timer = None
            self.working.update("")
            self.request_render()

    async def _working_timer_loop(self) -> None:
        while True:
            await asyncio.sleep(0.08)
            self._tick_working()

    def _tick_working(self):
        """Advance the shimmer one step and refresh the active task label."""
        store = state_store.get_todos(self._project_dir) or {}
        active = next(
            (t for t in store.values() if t.get("status") == "in_progress"), None
        )
        label = (
            (active.get("activeForm") or active.get("content")) if active else "Working"
        )
        self._spin_i += 1
        self.working.update(shimmer(f"{label}…", self._spin_i, theme_store_.get("banner_dim"), theme_store_.get("fg")))
        self.request_render()

    # ============================================= terminal busy and status of terminal logic

    # ============================================= loading /reloadfing messages

    REWIND_LIMIT = 50

    def get_rewind_options(self, messages):
        """Return (index, label) pairs of user messages for rewind picker."""
        options = []
        for i, msg in enumerate(messages):
            if msg.get("role") != "user":
                continue
            content = msg.get("content", "")
            if not isinstance(content, str) or content.strip().startswith(
                "<system-reminder>"
            ):
                continue
            options.append((i, content.strip().replace("\n", " ")[:100]))
        return options[-self.REWIND_LIMIT :]

    async def _reload_messages_from_disk(self):
        """Reload message list from disk when another writer appends (e.g., /gui)."""
        all_msgs = load_msgs(self._project_dir)

        container = self.message_list
        container.clear()
        self._msg_rows = []
        self._finalize_streaming()

        history_msgs = history_mount_(all_msgs)
        for m in history_msgs:
            self.message_list.add(MessageRow(m))
        self._msg_rows = history_msgs
        self.messages_scroll.scroll_to_end()
        self.request_render()

    async def _resume_after_gui(self) -> None:
        """Reload from disk, then drain queued items (must be sequenced)."""
        await self._reload_messages_from_disk()
        from micro_cc.webui import session as gui_session
        for content in gui_session.drain_incoming():  # unclaimed wakeups go back to the terminal
            await self._inject_incoming_turn(content)
        if self._input_mode != "query_active":  # a drained wakeup already started a turn; its finally drains the rest
            await self._drain_next_queued()

    # ============================================= loading /reloadfing messages

    # ============================================= prompt mechanics files slash pickers

    def _on_prompt_changed(self, text: str) -> None:
        """Typing drives the live panel: slash commands first, then @file mentions."""
        if not (self._open_slash_panel(text) or self._open_at_panel()):
            self._live_panel.close()

    def _open_slash_panel(self, text: str) -> bool:
        if not text.startswith("/") or "\n" in text:
            return False
        matches = [(c, d) for c, d in mods_.slash_command_info() if c.startswith(text)]
        if not matches:
            return False
        self._live_panel.open([PanelItem(cmd, f"{cmd:<12}{desc}") for cmd, desc in matches], on_select=self._on_slash_picked)
        return True

    _AT_MENTION_MAX_RESULTS = 50

    def _current_mention_query(self) -> str | None:
        """Text after nearest unspaced '@' on current line, up to cursor."""
        row, col = self.prompt.cursor_location
        line = self.prompt.get_line(row)[:col]
        at_idx = line.rfind("@")
        if at_idx == -1:
            return None
        fragment = line[at_idx + 1:]
        if any(c.isspace() for c in fragment):
            return None
        return fragment

    def _open_at_panel(self) -> bool:
        query = self._current_mention_query()
        if query is None:
            return False
        matches = rank_file_matches(self._project_files, query, self._AT_MENTION_MAX_RESULTS)
        if not matches:
            return False
        self._live_panel.open([PanelItem(f, f) for f in matches], on_select=lambda it: self.insert_at_mention(it.value))
        return True

    def insert_at_mention(self, file_path: str):
        """Replace the '@query' fragment under the cursor with file_path."""
        row, col = self.prompt.cursor_location
        line = self.prompt.get_line(row)
        at_idx = line.rfind("@", 0, col)
        if at_idx != -1:
            self.prompt.replace(file_path, (row, at_idx), (row, col))
        self.set_focus(self.prompt)

    def action_scroll_messages_up(self, lines: int = 3):
        self.messages_scroll.scroll_by(-lines)
        self.request_render()

    def action_scroll_messages_down(self, lines: int = 3):
        self.messages_scroll.scroll_by(lines)
        self.request_render()

    def action_scroll_messages_page_up(self):
        self.messages_scroll.scroll_by(-self.messages_scroll.viewport_height)
        self.request_render()

    def action_scroll_messages_page_down(self):
        self.messages_scroll.scroll_by(self.messages_scroll.viewport_height)
        self.request_render()

    def action_scroll_messages_top(self):
        self.messages_scroll.scroll_to_start()
        self.request_render()

    def action_scroll_messages_bottom(self):
        self.messages_scroll.scroll_to_end()
        self.request_render()

    # ============================================= prompt mechanics files slash pickers

    # ============================================= ESC

    def action_cancel_query(self):
        # Checked first: the bgproc detail view is orthogonal to _input_mode; Esc just closes it.
        if self._bgproc_viewing:
            self._hide_bgproc_detail()
            return

        # /gui handed the session to the browser; ESC takes it back.
        if self._input_mode == "gui":
            from micro_cc.tui_native.screen_cmds_ import stop_gui

            if stop_gui(self):
                self._safe_task(self._resume_after_gui(), "resume after gui")
                self._flash_status("⇤ GUI stopped — terminal is live again")
            return

        if self._input_mode == "login":
            self._option_panel.close()
            self._login_stage = None
            self._login_provider = None
            self._login_values = {}
            self._input_mode = "idle"
            self.prompt.placeholder = ""
            self.set_focus(self.prompt)
            return

        if self._input_mode == "keys":
            self._keys_stage = None
            self._keys_pending_name = None
            self._keys_values = {}
            self._input_mode = "idle"
            self.prompt.placeholder = ""
            self.set_focus(self.prompt)
            return

        # Open panels cancel themselves; their on_cancel wakes any waiter.
        for panel in (self._option_panel, self._live_panel):
            if panel.is_open:
                panel.cancel()
                return

        # Mid-approval ESC rejects just this tool; a second ESC (query_active) interrupts.
        if self._input_mode == "awaiting_approval":
            if self._pending_input is not None and not self._pending_input.is_set():
                self._pending_result = False
                self._pending_input.set()
                return

        if self._input_mode == "question_asked":
            if self._pending_input is not None and not self._pending_input.is_set():
                self._pending_result = None
                self._pending_input.set()
                return

        # Idle with nothing pending: Esc-Esc clears the prompt (a stray Esc must not log "interrupted").
        if self._input_mode == "idle":
            if not self.prompt.text:
                return
            if self._esc_clear_armed:
                self.prompt.clear()
                self._esc_clear_armed = False
            else:
                self._esc_clear_armed = True
                self._flash_status(
                    "Esc again to clear", seconds=self._ESC_CLEAR_TIMEOUT
                )
                if self._esc_clear_timer_task is not None:
                    self._esc_clear_timer_task.cancel()
                self._esc_clear_timer_task = self._call_later(self._ESC_CLEAR_TIMEOUT, self._disarm_esc_clear)
            return

        # Cancel the query task; the UI cleanup below is sync for instant feedback.
        if self._current_query_task is not None and not self._current_query_task.done():
            self._current_query_task.cancel()
            self_reload_.cancel_request()  # an interrupt withdraws a queued reload; the model can ask again

        if self._pending_input is not None:
            self._pending_input.set()
        self._pending_input = None
        self._pending_result = None
        self._input_mode = "idle"
        if self._loop_msgs is not None:
            if self._streaming is not None:
                partial = self._streaming.get_content().strip()
                # A tool turn already carries its text in the flushed assistant message.
                if partial and self._loop_msgs[-1:] and self._loop_msgs[-1].get("role") != "assistant":
                    self._loop_msgs.append({"role": "assistant", "content": partial})
            self._finalize_streaming()
            store_msgs(self._project_dir, self._loop_msgs)
        err = {"type": "error", "content": " interrupted"}
        self._msg_rows.append(err)
        self.message_list.add(MessageRow(err))
        self._scroll_to_bottom()

        for _, row, msg, _ in self._queue:
            self.message_list.remove(row)
            if msg in self._msg_rows:
                self._msg_rows.remove(msg)
        self._queue.clear()

        self._set_busy(False)
        self.set_focus(self.prompt)

    # ============================================= ESC

    # ============================================= do query

    async def _on_prompt_submitted(self, text: str, literal: bool = False, skip_mods: bool = False) -> None:
        """Handle a submitted prompt: slash commands, login/keys input, image paths, or a new turn."""
        query = text.strip()
        query = re.sub(
            r'⟪paste:(\d+)\|\d+ chars, \d+ lines⟫',
            lambda m: self.prompt._paste_store.get(int(m.group(1)), m.group(0)),
            query,
        )
        self.prompt._paste_store.clear()
        self.prompt._paste_id = 0

        # The browser owns the conversation during /gui; two writers would corrupt the jsonl.
        if self._input_mode == "gui":
            self.prompt.clear()
            self._flash_status(f"▸ GUI has the session — {self._gui_url} · esc to stop")
            return

        if self._input_mode == "login":
            login_ui_.handle_login_input(self, query)
            self.prompt.clear()
            return

        if self._input_mode == "keys":
            login_ui_.handle_keys_input(self, query)
            self.prompt.clear()
            return

        if self._input_mode == "question_asked":
            if self._pending_input is not None and not self._pending_input.is_set():
                question_ui_.advance(self, query)
            self.prompt.clear()
            return

        if not query:
            return

        if self._whats_new_row is not None:
            self.message_list.remove(self._whats_new_row)
            self._whats_new_row = None
            whats_new.mark_seen()

        # Prefix commands take free text after the name, e.g. "/new-skill do X".
        lower_q = query.lower()
        mod_cmd = None if literal or skip_mods else mods_.match_command(query)
        matched_prefix = None if literal or mod_cmd else next(
            (p for p in PREFIX_COMMANDS if lower_q == p or lower_q.startswith(p + " ")),
            None,
        )
        handler = None if literal or mod_cmd or matched_prefix is not None else SLASH_COMMANDS.get(lower_q)

        # Gate builtins mid-stream: they mutate message_list/_loop_msgs that the in-flight turn writes to.
        # Mod commands only reach the api, whose send_prompt queues, so they run mid-stream.
        if (matched_prefix is not None or handler is not None) and self._input_mode == "query_active":
            self._flash_status(
                "⏳ still streaming — esc to interrupt, then retry the command", seconds=4
            )
            return

        if mod_cmd is not None:
            name, arg = mod_cmd
            self.prompt.clear()
            builtins = {c["name"] for c in command_registry.COMMANDS}

            async def builtin(e):
                # nxt(e) on a command that shadows a builtin runs the builtin.
                if "/" + e["name"] in builtins:
                    await self._on_prompt_submitted(e["line"], skip_mods=True)

            try:
                await mods_.adispatch("command", {"name": name, "arg": arg, "line": query}, builtin, name=name)
            except Exception as e:
                await self._mount_row({"type": "error", "content": f"/{name} failed: {type(e).__name__}: {e}"})
            return

        if matched_prefix is not None:
            extra = query[len(matched_prefix):].strip()
            self.prompt.clear()
            await PREFIX_COMMANDS[matched_prefix](self, extra)
            return

        if handler is not None:
            await handler(self)
            self.prompt.clear()
            return

        # Encode image paths now so they ride this turn as content blocks.
        query, image_paths = extract_image_paths(query)
        encoded_image = [e for e in (sanitize_and_encode_image_(p) for p in image_paths) if e] or None
        if image_paths and not query:
            names = ", ".join(os.path.basename(p) for p in image_paths)
            query = f"[image(s) attached: {names}]"

        if self._input_mode == "query_active":
            msg = {"type": "user_queued", "content": query}
            row = await self._mount_row(msg)
            self._queue.append((query, row, msg, encoded_image))
            self.prompt.clear()
            return

        await self._mount_row({"type": "user", "content": query})
        self.prompt.clear()

        if not any_configured():
            err = {"type": "error", "content": "No API endpoint configured — run /login"}
            await self._mount_row(err)
            return

        # The Ollama daemon can die or the tag be removed between logins.
        if active_provider() == "ollama":
            base_url = os.getenv("OLLAMA_BASE_URL")
            model = os.getenv("OLLAMA_MODEL", "")

            if not await asyncio.to_thread(ollama_daemon_up, base_url):
                await self._mount_row({
                    "type": "text",
                    "content": "∴ Ollama daemon not running — starting it...",
                })
                await asyncio.to_thread(start_ollama_daemon)

            error = check_ollama(model, base_url)
            if error:
                await self._mount_row({
                    "type": "error",
                    "content": f"{error}\n\nFalling back to /login — reconfigure Ollama below.",
                })
                self._login_provider = "ollama"
                self._login_stage = model_catalog.login_sequence("ollama")[0]
                self._login_values = {}
                self._input_mode = "login"
                self.prompt.placeholder = model_catalog.login_prompt(self._login_stage)
                self.prompt.clear()
                self._refresh_status()
                return

        self._input_mode = "query_active"
        self._current_query_task = self._safe_task(self.do_query(query, encoded_image=encoded_image), "do_query")

    def _get_ui(self):
        if getattr(self, "_user_ui", None) is None:
            self._user_ui = make_ui(self)
        return self._user_ui

    async def _drain_next_queued(self) -> bool:
        """Pop the next queued turn and start it; True if one was started.
        Called from do_query's finally and _resume_after_gui."""
        if not self._queue:
            return False
        # Re-claim before any await so a mid-mount submit can't race a second worker.
        self._input_mode = "query_active"
        next_q, next_row, next_msg, next_image = self._queue.popleft()
        # Re-mount the queued row at the end; it may already be gone.
        self.message_list.remove(next_row)
        if next_msg in self._msg_rows:
            self._msg_rows.remove(next_msg)
        await self._mount_row({"type": "user", "content": next_q})
        self._current_query_task = self._safe_task(self.do_query(next_q, encoded_image=next_image), "do_query")
        return True

    async def do_query(self, query, encoded_image=None) -> None:
        self._set_busy(True)
        self._loop_msgs = load_msgs(self._project_dir)
        container = self.message_list

        tool_call_rows = {}
        approval_rows = {}

        sleep_handle = _inhibit_sleep()
        try:
            async for event in claude_loop(
                query=query,
                msgs=self._loop_msgs,
                project_dir=self._project_dir,
                model=self._current_model,
                max_tokens=self._trim_budget,
                surface="tui",
                encoded_image=encoded_image,
            ):
                etype = event.get("type")
                etype_handler = ETYPES.get(etype)
                if etype_handler is not None:
                    await etype_handler(
                        self, event, container, tool_call_rows, approval_rows
                    )
                # After the builtin draw, so a mod can't delay or reorder it.
                mods_.observe(event)
        except asyncio.CancelledError:
            # action_cancel_query already did the UI cleanup; fall through to the idempotent finally.
            pass
        finally:
            _allow_sleep(sleep_handle)
            self._pending_input = None
            self._finalize_streaming()
            self._set_busy(False)
            self.set_focus(self.prompt)
            self._refresh_status()
            self._static_hint_text()
            self._input_mode = "idle"
            # Self-reload only when nothing is queued, so a queued prompt isn't lost.
            if not await self._drain_next_queued():
                await self_reload_ui_.maybe_self_reload(self)

    # ============================================= do query

    # ============================================= subagents

    async def _inject_incoming_turn(self, content: str) -> None:
        """Start a turn now if idle, queue if busy, or hand to the browser during /gui."""
        if self._input_mode == "gui":
            from micro_cc.webui import session as gui_session
            gui_session.push_incoming(content)
            return
        if self._input_mode == "query_active":
            msg = {"type": "user_queued", "content": content}
            row = await self._mount_row(msg)
            self._queue.append((content, row, msg, None))
            return
        await self._mount_row({"type": "user", "content": content})
        self._input_mode = "query_active"
        self._current_query_task = self._safe_task(self.do_query(content), "do_query")

    def _on_session_message(self, from_dir: str, text: str) -> None:
        """session_ipc_ socket callback; hands off to a task rather than doing async work inline."""
        self._safe_task(self._handle_session_message(from_dir, text), "incoming session message")

    def _on_monitor_event(self, content: str) -> None:
        """Wake callback for a monitor watch event; same task hand-off as _on_session_message."""
        self._safe_task(self._inject_incoming_turn(content), "monitor watch event")

    async def _handle_session_message(self, from_dir: str, text: str) -> None:
        """Deliver an incoming message_session_ call like a typed prompt."""
        await self._inject_incoming_turn(f"[incoming from {from_dir}]\n{text}")

    def _bgproc_picker_items(self, procs: list[dict], watches: list[dict] | None = None) -> list:
        """One PanelItem per background process or watch; description is the full command.
        Watch values are prefixed "watch:<id>" so they never collide with a PID."""
        items = []
        for p in procs:
            desc = f"{p['command']} · in {p['cwd']}" if p.get("cwd") else p["command"]
            items.append(PanelItem(str(p["pid"]), f"PID {p['pid']} · {p['age']}s", desc))
        for w in (watches or []):
            items.append(PanelItem(
                f"watch:{w['watch_id']}",
                f"◎ {w['watch_id']} · {w['age']}s",
                f"{w['description']} · {w['command']}",
            ))
        return items

    def _show_bgproc_detail(self) -> None:
        """ctrl+b: show background processes and watches in the prompt slot; leaves _input_mode alone."""
        from micro_cc.tools.bash_tool import list_background_processes
        from micro_cc.tools.monitor_watch_ import list_watches

        procs = list_background_processes()
        watches = list_watches()
        if not procs and not watches:
            self._flash_status("no background processes or watches", seconds=2.5)
            return
        items = self._bgproc_picker_items(procs, watches)
        self._bgproc_viewing = True
        self._option_panel.max_visible, self._option_panel.max_desc_lines = panel_layout_.fit(self, items)
        # Read-only peek: Enter and Esc both dismiss.
        self._option_panel.open(items, on_select=lambda _it: self._hide_bgproc_detail(), on_cancel=self._hide_bgproc_detail)

    def _hide_bgproc_detail(self) -> None:
        self._bgproc_viewing = False
        self._option_panel.close()

    def _refresh_bgproc_detail(self, procs: list[dict], watches: list[dict] | None = None) -> None:
        """Keep the open detail view live off the 3s tick; auto-close when nothing is left."""
        watches = watches or []
        if not procs and not watches:
            self._hide_bgproc_detail()
            return
        current = self._option_panel.get_selected_item()
        self._option_panel.set_items(self._bgproc_picker_items(procs, watches))
        if current is not None:
            self._option_panel.set_selected_value(current.value)
        self.request_render()

    _SUBAGENT_CHECKPOINT_STATUSES = {"DONE", "FAILED", "PAUSED", "NEEDS_INPUT"}
    # Truly final; PAUSED/NEEDS_INPUT still expect a relaunch, so they are never prunable.
    _SUBAGENT_TERMINAL_STATUSES = {"DONE", "FAILED"}

    def enter_subagent_view(self, target: str) -> None:
        """Show a subagent's conversation, read off disk, in the message area."""
        self._subagent_viewing_target = target
        self.subagent_message_list.clear()
        target_msgs = load_msgs(target)
        history_msgs = history_mount_(target_msgs)
        for m in history_msgs:
            self.subagent_message_list.add(MessageRow(m))
        self.main_split.left = self.subagent_scroll_view
        self.subagent_scroll_view.scroll_to_end()
        self.request_render()

    def exit_subagent_view(self) -> None:
        """Back to boss; reload from disk unless mid-turn, where the streaming row would reorder."""
        self._subagent_viewing_target = None
        self.main_split.left = self.messages_scroll
        self.messages_scroll.scroll_to_end()
        self.request_render()
        turn_active = self._current_query_task is not None and not self._current_query_task.done()
        if not turn_active:
            self._safe_task(self._reload_messages_from_disk(), "reload after subagent view exit")

    # Status-agnostic: /graph and the graph-orchestration skill hold the mechanism.
    _SUBAGENT_WAKEUP_REMINDER = (
        "You have subagents running — spawned via bash_ + microcc-headless. "
        "Call monitor_(action='check', targets=[project_dirs...]) for a full "
        "status check across all of them, and check GRAPH_PLAN.md in this project "
        "directory for what this means and what to do next. If you need the "
        "full orchestration mechanism again (tracking/wakeup/coordination "
        "details), call read_skill('graph-orchestration')."
    )

    # Cold-start prefix: after a restart the model may have no memory, so reloading the skill is mandatory.
    _SUBAGENT_COLD_START_PREFIX = (
        "[this is the first check since boss (re)started — you may have no "
        "live memory of this orchestration even if it's mid-flight] Call "
        "read_skill('graph-orchestration') now, before deciding anything, "
        "to reload the full mechanism — then read GRAPH_PLAN.md for the "
        "actual plan. Do not act on the checkpoint below until you have.\n\n"
    )

    # ============================================= subagents

    # ========================================================================== start | stop

    async def start(self):
        # Start background services
        state_store.start_cleanup_task()
        self._project_files = list_project_files(self._project_dir)
        self._session_server = await start_listener(
            self._project_dir, self._on_session_message
        )

        # Watch events push via this callback; only an interactive loop can receive them.
        from micro_cc.tools import monitor_watch_ as _monitor_watch_

        _monitor_watch_.set_wake_callback(self._on_monitor_event)

        # Scheduled prompts push like watch lines; set_start_tick lets a late-added schedule start the tick.
        from micro_cc.tools import monitor_schedule_runtime as _schedule_rt

        # A /theme switch repaints the running UI via this listener (see _apply_theme).
        theme_store_.on_change(self._apply_theme)
        _schedule_rt.set_wake_callback(self._on_monitor_event)
        _schedule_rt.set_start_tick(lambda: pollers_.ensure_schedule_tick(self))
        pollers_.ensure_schedule_tick(self)

        # Resume tracked subagents from disk and poll at once; prune now, since nothing can race a wakeup yet.
        from micro_cc.utils import subagent_tracker_ as _subagent_tracker_

        _subagent_tracker_.prune(self._project_dir)
        self._subagent_poll_timer = asyncio.create_task(pollers_.subagent_poll_loop(self))
        self._safe_task(pollers_.poll_subagents_tick(self, cold_start=True), "subagent poll")

        # Cheap in-memory check; a plain 3s interval is fine.
        self._bgproc_poll_timer = asyncio.create_task(pollers_.bgproc_poll_loop(self))
        pollers_.poll_bgprocs_tick(self)

        # Own always-on timer: the review recap isn't tied to the per-round status refresh.
        self._memory_review_poll_timer = asyncio.create_task(pollers_.memory_review_poll_loop(self))

        # Restart-on-self-change: baseline the harness source and poll it, on every install type.
        statusline_.migrate()
        self_reload_ui_.init_self_manifest(self)
        self._self_poll_timer = asyncio.create_task(self_reload_ui_.self_poll_loop(self))

        # Restore a prompt stashed by a self-reload; read-and-delete so it lands once.
        from micro_cc.utils import self_reload_ as _self_reload

        _stashed = _self_reload.take_pending_prompt(self._project_dir)
        if _stashed:
            self.prompt.insert(_stashed)

        self._refresh_status()
        self._static_hint_text()

        history_msgs = history_mount_(self.__existing_msgs)
        for m in history_msgs:
            self.message_list.add(MessageRow(m))
        self._msg_rows = history_msgs
        note = whats_new.pending()
        if note:
            acc = theme_store_.get("accent")
            body = "\n".join(f"  [dim]·[/dim] {_markup_escape(l)}" for l in note["lines"])
            self._whats_new_row = RichStatic(f"[bold {acc}]✦ micro-cc {note['version']} just landed ✦[/bold {acc}] [dim]what's new[/dim]\n{body}")
            self.message_list.add(self._whats_new_row)
        self.messages_scroll.scroll_to_end()

        cut = cut_short_calls(self.__existing_msgs)
        if cut:
            warn = theme_store_.get("warn")
            names = ", ".join(dict.fromkeys(cut))
            self._call_later(1.0, lambda: self._flash_status(
                f"[{warn}]↻ last run was cut off during {names}: marked interrupted, Claude will check state[/{warn}]",
                seconds=8,
            ))

        for _s in model_catalog.CATALOG_STATUS["skipped"]:
            mods_.record_error("catalog.json", _s)
        _uerrs = mods_.errors()
        if self._legacy_moved:
            _uerrs.insert(0, f"moved old {', '.join(self._legacy_moved)} to ~/.micro-cc/legacy/ (now mods/, ask Claude to port)")
        if _uerrs:
            _warn = theme_store_.get("warn")
            _note = _markup_escape(f"mods: {len(_uerrs)} notice(s), first: {_uerrs[0]}")
            self._call_later(9.5 if cut else 1.0, lambda: self._flash_status(
                f"[{_warn}]{_note}[/{_warn}]", seconds=8,
            ))

        # Focus the prompt before the first frame so Enter reaches _on_prompt_submitted.
        self.set_focus(self.prompt)

        # Background update check; must not delay first paint.
        self._safe_task(check_and_update(self, __version__), "update check")
        self._safe_task(_background_terminal_setup_check(self), "terminal setup check")

        # Take the terminal only on a tty; piped stdin still renders and takes wakeups.
        if sys.stdin.isatty():
            sys.stdout.write(ENTER_ALT_SCREEN)
            sys.stdout.flush()
            self.terminal.start(self._on_terminal_input, self._on_terminal_resize)
            asyncio.get_event_loop().add_reader(sys.stdin.fileno(), self._on_stdin_readable)
            self._stdin_reader_registered = True

        self._running = True
        self._render_task = asyncio.create_task(self._render_loop())
        self.request_render()

        # App is up: tell the supervisor so the boot-failure counter is cleared (see self_heal_).
        try:
            from micro_cc import self_heal_

            self_heal_.note_boot_ok(self._project_dir)
        except Exception:
            pass

    # ========================================================================== start

    # ==========================================================================

    async def stop(self):
        # No OSC 9;4 clear needed: _set_busy(False) cleared it, and terminals clear it on exit.
        self._running = False
        # Kill watches: unlike bash_ background jobs, nothing will read their events after exit.
        from micro_cc.tools import monitor_watch_ as _monitor_watch_

        _monitor_watch_.kill_all()
        if self._render_task is not None:
            self._render_task.cancel()
        if self._stdin_reader_registered:
            asyncio.get_event_loop().remove_reader(sys.stdin.fileno())
            self._stdin_reader_registered = False
            self.terminal.stop()
            sys.stdout.write(EXIT_ALT_SCREEN)
            sys.stdout.flush()
        state_store.stop_cleanup_task()
        if self._session_server is not None:
            stop_listener(self._project_dir, self._session_server)
        if self._subagent_poll_timer is not None:
            self._subagent_poll_timer.cancel()
        if self._bgproc_poll_timer is not None:
            self._bgproc_poll_timer.cancel()
        if self._memory_review_poll_timer is not None:
            self._memory_review_poll_timer.cancel()
        if self._self_poll_timer is not None:
            self._self_poll_timer.cancel()
        if self._schedule_poll_timer is not None:
            self._schedule_poll_timer.cancel()
        if self._gui_shutdown is not None:
            self._gui_shutdown()


from importlib.metadata import version as _pkg_version

__version__ = _pkg_version("micro-cc")


def start_():
    """Entry point; runs until app._running is cleared (/exit, /quit) or Ctrl+C."""
    if len(sys.argv) > 1:
        project_dir = os.path.abspath(sys.argv[1])
    else:
        project_dir = os.getcwd()

    existing_msgs = load_msgs(project_dir)
    app = MicroTui(project_dir, messages=existing_msgs)

    async def _run():
        await app.start()
        try:
            while app._running:
                await asyncio.sleep(1)
        finally:
            await app.stop()

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    try:
        start_()
    except Exception:
        # print is neutered at module level; log to stderr and a file so the crash is visible.
        import traceback
        from datetime import datetime

        tb = traceback.format_exc()
        _real_print(tb, file=sys.stderr)
        try:
            log_path = os.path.expanduser("~/.micro-cc/startup_crash.log")
            os.makedirs(os.path.dirname(log_path), exist_ok=True)
            with open(log_path, "a") as f:
                f.write(f"[{datetime.now().isoformat()}]\n{tb}\n")
        except OSError:
            pass
        raise
