import asyncio
import traceback
from optparse import check_builtin
import os
import re
import subprocess
import threading
import sys
import platform

from rich.jupyter import display
from rich.markup import escape as _markup_escape
from micro_cc.utils.history_mount_ import history_mount_
from micro_cc.utils.msg_store_ import load_msgs, rewind_msgs, store_msgs
from micro_cc.utils.session_ipc_ import start_listener, stop_listener
from micro_cc.utils.helpers import (
    has_configured_endpoint,
    apply_login,
    apply_project_keys,
    check_ollama,
    check_openrouter,
    get_endpoint,
    ollama_daemon_up,
    start_ollama_daemon,
    compute_ollama_trim_budget,
    extract_image_paths,
    sanitize_and_encode_image_,
)
from micro_cc.cache import state_store
from micro_cc.utils import banner_
from micro_cc.utils import theme_store_

# Theme tokens used inline in markup strings are read via theme_store_.get()
# right where they're interpolated — never bound to a module constant, which
# would freeze the palette at import and make a live /theme switch a no-op.
from micro_cc.tui_native.screen_cmds_ import SLASH_COMMANDS, PREFIX_COMMANDS, _pending_subagents
from micro_cc.utils import command_registry
from micro_cc.tui_native.self_update_ import check_and_update
from micro_cc.tui_native.self_terminal_setup_ import _background_terminal_setup_check
from micro_cc.utils.terminal_setup import mark_multiline_used, newline_hint_text
from micro_cc.tui_native.etype_handler_ import ETYPES
from micro_cc.utils.project_files_ import list_project_files, rank_file_matches
from micro_cc.utils.claude_subscription import (
    oauth_login_flow,
    discover_claude_code_token,
)
from micro_cc.models import registry as model_registry
from micro_cc.utils import hints, settings_store_
from micro_cc.utils.tokenization_simple import token_stats, load_token_stats
from micro_cc.claude_loop_ import claude_loop
from collections import deque
import random
from micro_cc.tui_native.alt_screen_ import TuiAltScreen, OverlayOptions, ENTER_ALT_SCREEN, EXIT_ALT_SCREEN
from micro_cc.tui_native.stack_ import VStack
from micro_cc.tui_native.scroll_view_ import ScrollView
from micro_cc.tui_native import list_picker_
from micro_cc.tui_native.list_picker_ import ListPicker, PickerItem
from micro_cc.tui_native.multi_select_list_ import MultiSelectList
from micro_cc.tui_native.question_panel_ import QuestionPanel
from micro_cc.tui_native.message_row_ import MessageRow, StreamingRow, RichStatic, HRule
from micro_cc.tui_native.prompt_input_ import PromptInput
from micro_cc.tui_native.terminal_ import ProcessTerminal
from micro_cc.tui_native.stdin_buffer_ import StdinBuffer, Paste
from micro_cc.tui_native.keys_ import parse_key, is_key_release
from micro_cc.utils.msg_store_ import load_checkpoint, load_memory_review_recap

# --------------- background noise: discard, don't persist ---------------
# With no handler configured, stdlib `logging` falls back to its
# "lastResort" handler, which writes WARNING+ records straight to
# sys.stderr. Any library that logs on a background thread/task while
# this app owns the real terminal (raw mode + alt-screen) — most
# concretely asyncio.unix_events, which logs "pipe closed by peer or
# os.write(pipe, data) raised exception." whenever something keeps
# writing to a subprocess pipe after the peer died (a crashed browser
# tool's driver process, a backgrounded bash_ child) — lands those raw
# bytes wherever the hardware cursor happens to be, which reads as text
# spliced into whatever's drawn there (usually the prompt input, since
# that's where the cursor parks between frames). A NullHandler is enough
# to make `found` handlers > 0 for every logger (so lastResort never
# fires) without writing anything to disk — this ships to users via
# PyPI, so nothing here should be quietly accumulating a file on their
# machine. Bare print() is the same failure class via a different door
# (it never touches `logging` at all) — same fix, same reasoning.
# ProcessTerminal.write() (the TUI's own renderer) writes via
# sys.stdout.write()/.flush() directly, not print(), so neither of these
# can touch rendering — they only catch code that forgot it's running
# inside a TUI's alt screen instead of a normal CLI. This is strictly the
# fallback net for noise nobody specifically routed anywhere on purpose;
# errors that are actually actionable (a broken MCP server, a bad MCP
# config file, an unhandled exception in a background task) are handled
# at their own source instead, as a real row in the transcript — see
# _safe_task and _on_stdin_readable below, and mcp_client_.py/
# search_tool_.py's MCP loading paths.
import logging as _logging

_logging.getLogger().addHandler(_logging.NullHandler())
_logging.getLogger().setLevel(_logging.WARNING)

import builtins as _builtins

_real_print = _builtins.print


def _bg_print(*args, **kwargs):
    pass


_builtins.print = _bg_print

# --------------- sleep inhibitor ---------------
# macOS: caffeinate -i  (prevent idle sleep). No-op elsewhere (Linux/Docker
# containers don't idle-sleep, so there's nothing to inhibit).


def _inhibit_sleep():
    """Start preventing system idle sleep. Returns a handle to pass to _allow_sleep()."""
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


def _ollama_trim_budget(num_ctx: int = None, max_output: int = None) -> int:
    """MICRO_CC_TRIM_BUDGET env var wins if set (power-user escape hatch);
    otherwise derive from OLLAMA_NUM_CTX/OLLAMA_MAX_OUTPUT so the budget
    always fits the model's actual context window — see
    compute_ollama_trim_budget's docstring for why a fixed number can't be
    safe across different num_ctx choices."""
    override = os.getenv("MICRO_CC_TRIM_BUDGET")
    if override:
        return int(override)
    if num_ctx is None:
        num_ctx = int(os.getenv("OLLAMA_NUM_CTX", "32768"))
    if max_output is None:
        max_output = int(os.getenv("OLLAMA_MAX_OUTPUT", "4096"))
    return compute_ollama_trim_budget(num_ctx, max_output)


def _initial_trim_budget(model_alias: str):
    """Class-attribute default — covers launching straight into an already
    -configured Ollama session (no /login this run) as well as the other
    backends, which now derive from the active model's real context window
    (model_registry.trim_budget_for) instead of a single flat setting that
    couldn't tell a 200k-context Sonnet from a 1M-context gpt-5.6 apart.
    MICRO_CC_TRIM_BUDGET still wins outright when set, same escape hatch
    Ollama already had."""
    endpoint = get_endpoint()
    if endpoint == "Ollama":
        return _ollama_trim_budget()
    override = os.getenv("MICRO_CC_TRIM_BUDGET")
    if override:
        return int(override)
    backend = "openrouter" if endpoint == "OpenRouter" else None
    return model_registry.trim_budget_for(model_alias, backend=backend)

# ============================================================================
# Stack
# ============================================================================
#
# The heartbeat that drives everything: MicroTui._render_loop(), start_live_tui_.py:504-511:
# async def _render_loop(self) -> None:
#     while self._running:
#         if self._dirty:
#             self.tui.do_render()
#             self._dirty = False
#         await asyncio.sleep(self._RENDER_FRAME)   # ~30ms
# This loop runs the whole time the app is alive. Every ~30ms, if self._dirty is true, it calls self.tui.do_render() — a full re-render pass. request_render() (called from update_msg's caller path, keystrokes, etc.) just sets self._dirty = True; it doesn't render anything itself, it just arms the next tick of this loop to do it.
# Streaming text/thinking deltas are a separate concern: etype_handler_._stream_delta paces the typewriter reveal inline (small sleep between slices, same trick as webui/bridge.py's reveal()), calling request_render() after each slice so this loop's next tick picks up the partial content. No timer or buffer lives in the render loop itself for this.

# From do_render() down to your row, in DOM terms:

# TuiAltScreen.do_render()                              alt_screen_.py:720
#   └─ render_layout_frame(self.root, width, height)     alt_screen_.py:44
#        └─ self.root.render_in(width, height)           stack_.py:154  (root is a VStack)
#             └─ for each entry: entry.component.render(width)   stack_.py:143/159
#                  ├─ banner.render(width)                       ← RichStatic
#                  ├─ messages_scroll.render(width)               ← ScrollView
#                  │    └─ self.child.render(width)              scroll_view_.py:71
#                  │         (self.child IS self.message_list — same VStack)
#                  │         └─ message_list.render(width)        stack_.py:142
#                  │              └─ for each entry: entry.component.render(width)   stack_.py:143
#                  │                   ├─ MessageRow.render(width)   ← YOUR ROW, here
#                  │                   ├─ MessageRow.render(width)
#                  │                   └─ StreamingRow.render(width)  (if active)
#                  └─ bottom_bar.render(width)                   ← prompt etc.

# The render walk, mechanical, like the claude_loop one

# 1. _render_loop() wakes up (~30ms tick), sees self._dirty, calls self.tui.do_render(). (start_live_tui_.py:509)
# 2. do_render() calls render_layout_frame(self.root, width, height). self.root is a VStack. (alt_screen_.py:724, 44)
# 3. That calls self.root.render_in(width, height) — root has 3 entries: banner, messages_scroll, bottom_bar. (stack_.py:154)
# 4. render_in's body is [e.component.render(width) for e in self.entries] — a Python list comprehension, it literally calls .render(width) on each of those 3 objects, one by one, in order. (stack_.py:159)
# 5. When it gets to messages_scroll (a ScrollView), calling .render(width) on it runs ScrollView.render(), whose entire body is return self.child.render(width). self.child is self.message_list. (scroll_view_.py:71)
# 6. So that call becomes self.message_list.render(width) — same code as step 4, VStack.render() again, but now self.entries is your list of MessageRow/StreamingRow objects. (stack_.py:142-143)
# 7. For each of those, .render(width) is called — this is the literal line that calls your MessageRow.render(). For every row, every tick, whether or not it's currently scrolled on-screen.
# 8. Back up in message_list's VStack.render(), all the returned list[str] lines from every row get concatenated into one big list[str] — that's message_list's own return value.
# 9. Back in root's render_in, ScrollView.get_scrolled_lines(lines, h) slices that big list down to just the visible window — this is where scrolling actually happens, after every row already rendered. (stack_.py:172-179)
# 10. Root's 3 pieces (banner lines, windowed message lines, bottom_bar lines) get concatenated into one final list[str] — that's the whole screen, one string per terminal row.
# 11. do_render() diffs this against self.previous_screen line by line and writes only the changed rows to the actual terminal via ANSI cursor-position + clear-line codes.

class MicroTui():

    MODEL_OPTIONS = model_registry.MODEL_OPTIONS

    LOGIN_SEQUENCE = {
        "Anthropic": ["ANTHROPIC_API_KEY", "SERPAPI_KEY"],
        "Foundry": ["FOUNDRY_BASE_URL", "FOUNDRY_API_KEY", "SERPAPI_KEY"],
        "LiteLLM": ["LITELLM_BASE_URL", "LITELLM_API_KEY", "SERPAPI_KEY"],
        "OpenAI": ["OPENAI_API_KEY", "SERPAPI_KEY"],
        "OpenRouter": ["OPENROUTER_API_KEY", "OPENROUTER_MODEL", "SERPAPI_KEY"],
        "Ollama": [
            "OLLAMA_BASE_URL",
            "OLLAMA_MODEL",
            "OLLAMA_NUM_CTX",
            "OLLAMA_MAX_OUTPUT",
            "SERPAPI_KEY",
        ],
    }
    LOGIN_PROMPTS = {
        "ANTHROPIC_API_KEY": "Paste ANTHROPIC_API_KEY (sk-ant-...)",
        "OPENAI_API_KEY": "Paste OPENAI_API_KEY (chat backend)",
        "FOUNDRY_BASE_URL": "Paste your Foundry endpoint base URL",
        "FOUNDRY_API_KEY": "Paste your FOUNDRY_API_KEY",
        "LITELLM_BASE_URL": "Paste your LiteLLM proxy base URL",
        "LITELLM_API_KEY": "Paste your LITELLM_API_KEY",
        "OPENROUTER_API_KEY": "Paste OPENROUTER_API_KEY (openrouter.ai/keys)",
        "OPENROUTER_MODEL": (
            "Model slug, author/name — copy exactly from openrouter.ai/models\n"
            "e.g. qwen/qwen3-235b-a22b-thinking-2507, deepseek/deepseek-chat,\n"
            "  z-ai/glm-4.6, moonshotai/kimi-k2"
        ),
        "OLLAMA_BASE_URL": (
            "Ollama daemon URL.\n"
            "Requires the Ollama CLI + daemon running locally:\n"
            "  brew install ollama && brew services start ollama\n"
            "Enter for default: http://localhost:11434/v1\n"
            "(only change this if Ollama runs on another host/port)"
        ),
        "OLLAMA_MODEL": (
            "Model tag to use.\n"
            "Local: pull first with `ollama pull <tag>`, e.g. qwen3:14b\n"
            "  Browse sizes/tags per model at ollama.com/library\n"
            "  See what you've already pulled: `ollama list`\n"
            "Cloud (huge models, run on Ollama's servers not your machine):\n"
            "  tag ends in :cloud, e.g. kimi-k2.7-code:cloud\n"
            "  still needs `ollama pull <tag>:cloud` once, plus `ollama signin`\n"
            "  browse at ollama.com/search"
        ),
        "OLLAMA_NUM_CTX": (
            "Context window, in tokens — this is what sizes the KV cache.\n"
            "Enter for default: 32768\n"
            "micro-cc reserves ~6000 tokens off this for system prompt/tools/\n"
            "  dynamic context, then MAX_OUTPUT below, then whatever's left goes\n"
            "  to conversation history\n"
            "Stay above MAX_OUTPUT + ~8000 or history trimming hits its 2000-\n"
            "  token floor: history gets crushed and totals can still exceed\n"
            "  this window, which Ollama truncates silently rather than erroring\n"
            "Lower it (e.g. 8192) only if the model doesn't fit your RAM at\n"
            "  full context, and drop MAX_OUTPUT to match"
        ),
        "OLLAMA_MAX_OUTPUT": (
            "Max tokens generated per response.\n"
            "Enter for default: 4096\n"
            "Comes out of NUM_CTX above, on top of the ~6000-token reserve for\n"
            "  system prompt/tools/dynamic context — a bigger cap here leaves\n"
            "  less room for conversation history\n"
            "If NUM_CTX - MAX_OUTPUT < ~8000, history trimming hits its floor"
        ),
        "SERPAPI_KEY": "SERPAPI_KEY — optional, Enter to skip",
    }

    _current_model = settings_store_.get_setting("model")
    _trim_budget = _initial_trim_budget(_current_model)

    ALLOW_SELECT = True

    _ESC_CLEAR_TIMEOUT = 1.5
    _SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
    _RENDER_FRAME = 0.03  # cadence do_render is called at while the app is running
    _STATIC_HINT_LINE_MAXLEN = 200
    _STATUS_LINE_TIMEOUT = 0.3
    _STATUS_LINE_MAXLEN = 200
    _STATUS_SCRIPT_PATH = os.path.expanduser("~/.micro-cc/statusline.sh")

    ## IMPORTANT!
    _update_available = None
    _STATIC_HINT_LINE_MAXLEN = 200

    def __init__(self, project_dir: str, messages: list):
        self._project_dir = project_dir
        load_token_stats(project_dir)
        self.__existing_msgs = messages
        # Backs the @ mention picker — same file universe as the GUI's
        # (webui/server.py's /api/files), just loaded once at mount instead
        # of over HTTP. See project_files_.py.
        self._project_files = []
        self._session_server = None  # Unix socket for message_session_, see on_mount
        self._loop_msgs = None  # entire loop_msgs owned
        self._msg_rows: list[dict] = []  # ordered list of msg dicts (for /copy + cancel)
        # state machine
        self._input_mode = "idle"  # "idle" | "login" | "keys" | "awaiting_approval" | "question_asked" | "query_active" | "gui"
        # Set while /gui has handed the session to the browser; see cmd_gui.
        self._gui_url = None
        self._gui_shutdown = None
        ## user queries queueing
        self._queue = deque()
        ## tools gated behind an approval prompt (editable via /dangerous)
        self._dangerous_tools = set(settings_store_.get_setting("dangerous"))
        ## "working on…" nudge (above the input) while a query is active
        self._working_timer = None
        self._spin_i = 0
        # api keys — _login_stage is None, or the exact env-var name currently
        # being collected (see LOGIN_SEQUENCE); _login_values accumulates
        # {env_var_name: typed_value} across the /login flow
        self._login_stage = None
        self._login_provider = None
        self._login_values = {}
        # /keys — project-scoped secrets (see cmd_keys/_handle_keys_input).
        # _keys_stage is None | "name" | "value"; _keys_pending_name holds
        # the name while its value is being typed; _keys_values accumulates
        # {name: value} across the loop until a blank name ends it.
        self._keys_stage = None
        self._keys_pending_name = None
        self._keys_values = {}
        ## ask user
        self._ask_questions = None
        self._question_panel: QuestionPanel | None = None
        self._ask_stage = None
        self._ask_answers = {}
        ## Esc-Esc-to-clear (only while idle — see action_cancel_query)
        self._esc_clear_armed = False
        self._esc_clear_timer_task: asyncio.Task | None = None
        ## subagent orchestration — see on_mount/_poll_subagents_tick.
        # Populated from disk (survives restart) and refreshed every tick
        # (survives /clear — a different file, untouched by cmd_clear).
        self._tracked_subagents = []
        self._subagent_poll_cache = {}  # project_dir -> (mtime_ns, size) from last poll
        self._subagent_poll_timer = None
        self._subagent_viewing_target: str | None = None
        # Seeded from any checkpoint already on disk — not 0 — so a project
        # that compacted in a *previous* session doesn't flash "compacted
        # just now" on the very first status refresh of this launch (0 !=
        # a real as_of_index looked like a fresh compaction every time).
        # See _update_status_bar's compaction-flash check.
        _existing_checkpoint = load_checkpoint(self._project_dir)
        self._last_checkpoint_index = (
            _existing_checkpoint["as_of_index"] if _existing_checkpoint is not None else 0
        )
        ## background bash processes — see on_mount/_poll_bgprocs_tick.
        self._bgproc_poll_timer = None
        # True while bgproc_detail_picker occupies the prompt slot — see
        # _show_bgproc_detail/_hide_bgproc_detail (ctrl+b). Orthogonal to
        # _input_mode on purpose: unlike question_asked/awaiting_approval,
        # peeking at a backgrounded command's full text shouldn't block or
        # interrupt whatever turn is or isn't in flight underneath it.
        self._bgproc_viewing = False
        # Same seeding trick as _last_checkpoint_index just above — a
        # project reviewed by a *previous* session shouldn't flash "memory
        # reviewed" on this launch's very first poll. See
        # _poll_memory_review_tick and msg_store_.load_memory_review_recap.
        _existing_review = load_memory_review_recap(self._project_dir)
        self._last_memory_review_ts = _existing_review["ts"] if _existing_review is not None else None
        self._memory_review_poll_timer = None

        ## scheduled prompts — see tools/monitor_schedule_.py. The tick task
        # is created in start() and cancelled in stop(), same ownership as the
        # other poll timers. _schedule_poll_timer is set lazily by
        # _ensure_schedule_tick (a schedule can be added mid-session).
        self._schedule_poll_timer: asyncio.Task | None = None

        self._pending_input: asyncio.Event | None = None
        self._pending_result = None
        self._streaming: StreamingRow | None = None
        # Textual's workers.cancel_all() (what action_cancel_query's tail
        # branch used to call) implicitly cancelled whatever task was
        # running do_query — there's no framework doing that for a plain
        # asyncio.Task, so the task itself has to be tracked explicitly to
        # cancel it. Without this, ESC reset all the app-side state but
        # the do_query task just kept running claude_loop() to completion
        # regardless — the actual bug behind "ESC doesn't stop the stream".
        self._current_query_task: asyncio.Task | None = None
        self._update_available = None
        self._flash_status_timer_task: asyncio.Task | None = None
        self._memory_flash_timer_task: asyncio.Task | None = None

        ## restart-on-self-change (see utils/self_reload_). _reload_pending is
        ## set by the poll loop when the harness's own source changed on disk,
        ## and drained at a turn boundary (idle + focus back on the prompt) —
        ## never mid-turn. _self_manifest is the (mtime_ns, size) baseline;
        ## _self_poll_timer is the always-on interval task, cancelled in
        ## stop(). None manifest (non-source install) makes the poll a no-op.
        self._reload_pending = False
        self._reload_changed: set = set()
        self._self_manifest = None
        self._self_poll_timer: asyncio.Task | None = None
        self._restarting = False  # guards against a second restart mid-relaunch

        # --- render loop bookkeeping ---
        self._dirty = True
        self._running = False
        self._render_task: asyncio.Task | None = None
        self._last_render_crash: str | None = None  # see _render_loop's except branch
        self._focused = None  # set_focus/get_focus — see _on_terminal_input for real dispatch

        self.prompt = None

        # --- Phase 5: input loop -----------------------------------------
        self.terminal = ProcessTerminal()
        self._stdin_buffer = StdinBuffer()
        self._stdin_reader_registered = False

        self._build_widget_tree()

    # --- widget tree -----------------------------------------------------
    def _build_widget_tree(self) -> None:
        # banner() returns (markup, hue); the hue is kept so the hint bar
        # can tint to match this launch's wordmark.
        _banner_markup, self._banner_hue = banner_.banner()
        self.banner = RichStatic(_banner_markup)

        self.prompt = PromptInput()
        # handle_key("enter") calls on_submit synchronously (no await) —
        # _on_prompt_submitted needs to be async (it awaits do_query/
        # command handlers), so wrap it in a task rather than assigning
        # the coroutine function directly.
        self.prompt.on_submit = lambda text: self._safe_task(self._on_prompt_submitted(text), "prompt submit")
        self.prompt.on_change = self._on_prompt_changed
        self.message_list = VStack()
        self.messages_scroll = ScrollView(self.message_list, follow="end", primary=True)
        # Separate VStack — enter_subagent_view/exit_subagent_view fill
        # THIS one with a subagent's own messages and swap root's
        # messages_scroll entry for subagent_scroll_view (VStack.replace).
        # Sharing self.message_list between both ScrollViews would mean
        # both views show the same content no matter which is swapped in.
        self.subagent_message_list = VStack()
        self.subagent_scroll_view = ScrollView(
            self.subagent_message_list, follow="end", primary=False
        )

        self.working = RichStatic("", single_line=True)
        self.ask_question_text = RichStatic("")  # genuinely multi-line — full question text wraps
        self.statusbar = RichStatic("", single_line=True)
        # Own line, own timer (self._memory_flash_timer_task) — deliberately
        # NOT sharing statusbar/_flash_status with the compaction flash. See
        # _flash_memory_review's docstring: review runs right after a
        # compaction fold (msg_store_.compact_checkpoint), so the two flashes
        # can legitimately be in flight at the same time, and _flash_status
        # has exactly one shared slot — reusing it here would let whichever
        # fires second cancel and stomp the other before its own timeout.
        # max_lines=5, not single_line — grows/shrinks with the actual recap
        # text like every sibling here (0 rows when empty, see
        # RichStatic.render), just bounded so a long change_log can't take
        # over the screen; overflow past 5 lines gets a trailing "…".
        # Lives in the "above prompt" group below (with working/
        # ask_question_text), not down by statusbar — see bottom_bar.add
        # order.
        self.memory_flash = RichStatic("", max_lines=5)
        self.static_hintbar = RichStatic("", single_line=True)
        # NOT single_line — these legitimately grow to one row per tracked
        # item (see _poll_subagents_tick/_poll_bgprocs_tick's "\n".join(...)).
        self.subagent_status = RichStatic("")
        self.bgproc_status = RichStatic("")
        self.top_rule = HRule()
        self.bottom_rule = HRule()

        # Same shape as cc's own layout: an ambient "what's happening"
        # slot above the input (working-spinner text, or the current
        # question when ask_user_tool is active), a divider, the input
        # itself, another divider, then the persistent statusbar/hint/
        # subagent/bgproc lines below. Dividers are always present —
        # a stable visual anchor around the input regardless of what's
        # in the above/below slots at a given moment.
        self.bottom_bar = VStack()
        self.bottom_bar.add(self.working)
        self.bottom_bar.add(self.ask_question_text)
        self.bottom_bar.add(self.memory_flash)
        self.bottom_bar.add(self.top_rule)
        self.bottom_bar.add(self.prompt)
        self.bottom_bar.add(self.bottom_rule)
        self.bottom_bar.add(self.statusbar)
        self.bottom_bar.add(self.static_hintbar)
        self.bottom_bar.add(self.subagent_status)
        self.bottom_bar.add(self.bgproc_status)
        # Which component currently occupies the prompt's slot in
        # bottom_bar — self.prompt normally, swapped for ask_user_picker/
        # ask_user_select while a question is active (see
        # _swap_into_prompt_slot / _show_current_ask_question).
        self._active_prompt_slot_component = self.prompt

        self.root = VStack()
        # shrink=0 on banner/bottom_bar is the actual fix here: VStack's
        # default shrink=1 applies to EVERY entry including these, so once
        # a long conversation's true (unwindowed) intrinsic height exceeds
        # the terminal — which it does almost immediately — allocate_
        # stack_sizes' shrink pass squeezed all three entries
        # proportionally by weight (shrink * size), and messages_scroll's
        # huge intrinsic size doesn't protect banner/bottom_bar from also
        # losing rows. Confirmed directly: with 200 messages,
        # root.last_sizes came out [0, 22, 2] — banner gone entirely,
        # bottom_bar cut from ~9 rows to 2 (which is exactly "statusline
        # suppressed"). Textual's CSS equivalent is these being
        # height:auto (never shrinks below content) while MessagesScroll
        # is the only flexible region — shrink=0 is that same contract:
        # only messages_scroll may give up space, and it can safely give
        # up ALL of it since ScrollView.get_scrolled_lines already windows
        # its content correctly at any allocated height, including 0.
        self.root.add(self.banner, basis="auto", shrink=0)
        self.root.add(self.messages_scroll, grow=1)
        self.root.add(self.bottom_bar, basis="auto", shrink=0)

        self.tui = TuiAltScreen(self.root)
        self.tui.set_primary_scroll_view(self.messages_scroll)

        # Pickers — persistent component instances; shown/hidden as
        # overlays (self.tui.show_overlay/OverlayHandle.hide()) rather than
        # a Textual .display flag, since alt_screen_.py's overlay stack
        # already exists and covers this need (7x OptionList/SelectionList
        # pickers -> Picker(Component), one instance each).
        self.model_picker = ListPicker()
        self.model_picker.set_items(
            [PickerItem(o, o) for o in self._startup_model_options()]
        )
        self.rewind_picker = ListPicker()
        self.dangerous_picker = MultiSelectList()
        self.slash_picker = ListPicker()
        self.at_picker = ListPicker()
        self.login_picker = ListPicker()
        self.login_picker.set_items([
            PickerItem(name, name) for name in ("Anthropic", "Bad Bunny", "Foundry", "LiteLLM", "OpenAI", "OpenRouter", "Ollama")
        ])
        self.ask_user_picker = ListPicker()
        self.ask_user_select = MultiSelectList()
        self.bgproc_detail_picker = ListPicker()
        self.subagents_picker = ListPicker()  # items set per open — see screen_cmds_.cmd_subagents
        self.theme_picker = ListPicker()
        self.theme_picker.set_items([
            PickerItem(n, theme_store_.THEME_LABELS.get(n, n))
            for n in theme_store_.PRESETS
        ])

        self.model_picker.on_select = self._on_model_picked
        self.model_picker.on_cancel = lambda: self._close_picker("_model_picker_overlay")
        self.rewind_picker.on_select = self._on_rewind_picked
        self.rewind_picker.on_cancel = lambda: self._close_picker("_rewind_picker_overlay")
        self.dangerous_picker.on_confirm = self._on_dangerous_confirmed
        self.dangerous_picker.on_cancel = lambda: self._close_picker("_dangerous_picker_overlay")
        self.slash_picker.on_select = self._on_slash_picked
        self.slash_picker.on_cancel = lambda: self._close_picker("_slash_picker_overlay")
        self.at_picker.on_select = lambda item: self.insert_at_mention(item.value)
        self.at_picker.on_cancel = lambda: self._close_picker("_at_picker_overlay")
        self.login_picker.on_select = self._on_login_picked
        self.login_picker.on_cancel = lambda: self._close_picker("_login_picker_overlay")
        self.ask_user_picker.on_select = lambda item: self._advance_ask_user(item.label)
        self.ask_user_select.on_confirm = lambda checked: self._advance_ask_user([it.label for it in checked])
        # Enter and Escape both just dismiss — there's nothing to "select",
        # this is a read-only peek at a backgrounded command (see
        # _show_bgproc_detail/ctrl+b).
        self.bgproc_detail_picker.on_select = lambda item: self._hide_bgproc_detail()
        self.bgproc_detail_picker.on_cancel = self._hide_bgproc_detail
        self.theme_picker.on_select = self._on_theme_picked
        self.theme_picker.on_cancel = lambda: self._close_picker("_theme_picker_overlay")
        self.subagents_picker.on_select = self._on_subagents_picked
        self.subagents_picker.on_cancel = lambda: self._close_picker("_subagents_picker_overlay")

        self._model_picker_overlay = None
        self._rewind_picker_overlay = None
        self._dangerous_picker_overlay = None
        self._slash_picker_overlay = None
        self._at_picker_overlay = None
        self._login_picker_overlay = None
        self._theme_picker_overlay = None
        self._subagents_picker_overlay = None

    # --- render loop / focus ---------------------------------------------
    # Used everywhere below — foundational, not tied to any one feature.

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
                    # Unlike every other task here, this loop was never
                    # wrapped in _safe_task's try/except — an exception
                    # raised inside do_render() (e.g. rich.errors.MarkupError
                    # from unescaped "[" in dynamic content like a
                    # subagent's project_dir name or a backgrounded shell
                    # command — see monitor_.format_status_glyph
                    # and _poll_bgprocs_tick) used to propagate straight out
                    # of this `while self._running` loop and end the task for
                    # good: nothing ever calls do_render() again, so the
                    # screen just stops updating with no error shown — reads
                    # exactly like "the terminal froze." Catch and skip this
                    # tick instead, same safety net _safe_task already gives
                    # every other task.
                    self._dirty = False
                    # _show_error_row itself calls request_render(), which
                    # would set self._dirty back to True — if the same
                    # widget's content is still bad next tick (nothing here
                    # clears it), that reproduces the exact same crash and
                    # loops forever at one error row per frame. Report it
                    # once per distinct failure, not once per tick.
                    key = f"{type(e).__name__}: {e}"
                    if key != self._last_render_crash:
                        self._last_render_crash = key
                        tb = traceback.format_exc()
                        # The error row itself only ever showed type+message
                        # — no file/line, so a real crash was unguessable
                        # after the fact (see /clear's IndexError report: no
                        # way to tell which widget's render() actually threw
                        # without this). Full traceback goes to a durable log
                        # every time; the row itself adds just the one frame
                        # that's actually informative — where inside THIS
                        # codebase (not asyncio/site-packages plumbing) the
                        # exception originated — found by walking the
                        # traceback from the innermost frame outward for the
                        # first one whose filename is this file's own package
                        # dir, since do_render()'s own call stack is many
                        # frames deep through render()/get_scrolled_lines/
                        # _compose/etc. before it reaches the actual bug.
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
        """Append a real, permanent 'error' row to the transcript — same
        shape etype_handler_tui_.handle_error uses for a model-call
        failure. This is the shared landing spot for crashes that used to
        get written to a crash-log file on disk: this ships to users via
        PyPI, so nothing should be silently accumulating there, and a row
        in the actual conversation the user is already looking at is more
        useful than a file path they'll never open anyway."""
        err = {"type": "error", "content": text}
        self._msg_rows.append(err)
        row = MessageRow(err)
        self.message_list.add(row)
        self._scroll_to_bottom()
        self.request_render()

    def _safe_task(self, coro, label: str = "") -> asyncio.Task:
        """Every do_query/_resume_after_gui/command-handler task used to
        be a bare asyncio.create_task(...) — fire-and-forget with no
        exception handler. A bug anywhere inside one (the missing
        copy_to_clipboard being the concrete case that surfaced this) is
        an unhandled exception in a task nothing ever awaits, which
        asyncio logs by dumping the raw traceback to stderr — inside an
        alt-screen app that reads as a garbled frame, same failure class
        as _on_stdin_readable's try/except was already built to catch,
        just on a different path. This is that same safety net for
        task-based dispatch: surface the exception as a real row in the
        transcript and keep the app alive, instead of corrupting the
        screen (the old behavior) or writing a traceback file nobody
        shipped this way should be accumulating (the previous fix here)."""
        async def _wrapped():
            try:
                await coro
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self._show_error_row(f"{label or 'background task'} crashed: {type(e).__name__}: {e}")
        return asyncio.create_task(_wrapped())

    def set_focus(self, component) -> None:
        # _on_terminal_input reads self._focused to route keystrokes; this
        # also lets a focusable component (PromptInput) know to render its
        # own cursor marker (see prompt_input_.py's `focused` flag).
        if self._focused is not None and hasattr(self._focused, "focused"):
            self._focused.focused = False
        self._focused = component
        if component is not None and hasattr(component, "focused"):
            component.focused = True
        self.request_render()

    def get_focus(self):
        return self._focused

    # --- Phase 5: input loop -----------------------------------------------
    # select()/os.read() replaced by asyncio's own fd-readiness callback
    # (loop.add_reader) — same idea (don't block the loop waiting on
    # stdin), just using asyncio's native mechanism instead of hand-rolling
    # a select() loop, since everything else here already runs on asyncio.

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
            # A bug anywhere in this chain used to propagate out of
            # asyncio's add_reader callback, which asyncio logs by
            # dumping the raw traceback to stderr — inside an alt-screen
            # app that reads as a garbled, "distorted" frame, not an
            # error message. Catch it and surface it as a real row
            # instead (see _show_error_row).
            self._show_error_row(f"input handler crashed: {type(e).__name__}: {e}")

    def _handle_input_item(self, item) -> None:
        if isinstance(item, Paste):
            self.on_paste(item.text)
            self.request_render()
            return
        # feed_stdin() handles Kitty keyboard-protocol negotiation
        # internally and, when the sequence isn't part of that handshake,
        # calls self._on_terminal_input(item) itself (the on_input
        # callback passed to ProcessTerminal.start()) — that's the real
        # dispatch path, not a separate step here. See terminal_.py's
        # module docstring for this seam.
        self.terminal.feed_stdin(item)

    def _on_terminal_input(self, data: str) -> None:
        # Kitty keyboard protocol (negotiated in ProcessTerminal.start(),
        # flag 2 = report event types) sends press AND release as separate
        # sequences for every physical keystroke, sometimes a repeat event
        # too while held. parse_key() deliberately doesn't filter these
        # (it's a pure "what key is this" parser) — is_key_release/
        # is_key_repeat are the caller's job. Skipping this was the actual
        # cause of "one h renders three h": every physical keypress was
        # dispatched once for press AND once for release (and once more
        # per repeat tick), each treated as a fresh keystroke.
        if is_key_release(data):
            return
        mouse = self.tui.parse_sgr_mouse_event(data)
        if mouse is not None:
            # Wheel events (xterm SGR: button 64 = up, 65 = down) were
            # never handled — worse, handle_selection_mouse_event masks
            # with `button & 3`, and 64 & 3 == 0, the same as a plain
            # left-click. A wheel scroll was being misread as a click
            # press, not just ignored. Intercept before it ever reaches
            # the selection state machine.
            if mouse["button"] in (64, 65):
                if not mouse["release"]:
                    if mouse["button"] == 64:
                        self.action_scroll_messages_up()
                    else:
                        self.action_scroll_messages_down()
                return
            self.tui.handle_selection_mouse_event(mouse)
            # Drag auto-scroll lives in TuiAltScreen.autoscroll_tick (driven by
            # _render_loop) — selection is in content rows, so it survives scrolling.
            # Click-to-expand: on release, if handle_selection_mouse_event
            # didn't end up forming a real selection (anchor == focus —
            # exactly the "press and release landed at the same
            # character, this wasn't a drag" case; a genuine drag or a
            # double/triple-click word/line select both leave a real,
            # non-empty range here), resolve which component actually
            # owns that screen row via VStack/ScrollView.find_component_at
            # and toggle it if it's expandable. This is the hit-test
            # Textual used to provide for free — see MessageRow's own
            # toggle_expanded(), unchanged since window_overlay_.py, which
            # never had anything to call it here until now.
            if mouse["release"] and self.tui.get_selection_bounds() is None:
                component = self.root.find_component_at(mouse["y"])
                if component is not None and hasattr(component, "toggle_expanded"):
                    if component.toggle_expanded():
                        self.request_render()
            self.request_render()
            return
        key_id = parse_key(data)
        if key_id is None:
            return
        if key_id == "escape":
            # App-level binding, not widget-level — matches the old
            # Textual Binding("escape", "cancel_query", ...), which fired
            # regardless of what had focus. ListPicker.handle_key also has
            # its own "escape" branch (on_cancel) for when it's reused
            # outside this app, but action_cancel_query's state machine is
            # the authoritative, more complete path here (it already
            # closes pickers via close_all_pickers()), so route ESC here
            # unconditionally rather than through the focused component.
            self.action_cancel_query()
            self.request_render()
            return

        # Keyboard scroll fallback — same app-level bindings the old
        # Textual BINDINGS list had (ctrl+up/down/pageup/pagedown/home/
        # end -> action_scroll_messages_*), independent of what's
        # focused. The action_scroll_messages_* methods themselves have
        # existed since the very first pass at this file; nothing ever
        # called them until now, which is the actual reason scrolling
        # looked entirely dead (mouse wheel had the same problem — see
        # the button 64/65 handling above).
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

        # slash_picker/at_picker are "live filter while still typing"
        # overlays — they deliberately never take focus (see
        # _open_picker's steal_focus=False for these two, below), so
        # navigation keys have to be intercepted here explicitly instead
        # of relying on get_focus(). Everything else (printable chars,
        # backspace, etc.) still falls through to the focused prompt,
        # which is what keeps the filter live as you type.
        live_picker = (
            self.slash_picker if self._slash_picker_overlay is not None else
            self.at_picker if self._at_picker_overlay is not None else
            None
        )

        # Subagent view toggle — cycles [boss, *tracked subagents] one
        # step per press. self._tracked_subagents holds project_dir strings
        # (see _poll_subagents_tick), so the cycle itself is just that list
        # with None (boss) prepended — None here means "not currently
        # viewing a subagent", the same role undefined plays in
        # a viewingAgentTaskId.
        #
        # Only gated on focus-on-prompt + no live picker now — those are
        # real structural conflicts (a picker or some other focused widget
        # may want shift+tab for itself). The prompt-must-be-empty and
        # tracked-subagents-must-be-nonempty checks used to live in this
        # same top-level condition, which made both failure cases silent
        # no-ops: press shift+tab with a half-typed prompt, or with nothing
        # tracked yet, and nothing happened, no feedback, easy to conclude
        # the feature just doesn't work. Nothing in PromptInput binds
        # shift+tab, so there's no real conflict with typed text — dropped
        # that requirement entirely rather than just messaging around it.
        # The empty-tracked-list case still can't do anything, so it gets a
        # flash instead of silence.
        if (
            key_id == "shift+tab"
            and self.get_focus() is self.prompt
            and live_picker is None
        ):
            if not self._tracked_subagents:
                self._flash_status("no subagents to switch to", seconds=2.5)
                return
            cycle = [None, *self._tracked_subagents]
            try:
                current_idx = cycle.index(self._subagent_viewing_target)
            except ValueError:
                # Whatever we were viewing dropped out of the tracked list
                # (subagent finished and got pruned) — land back on boss.
                current_idx = 0
            next_target = cycle[(current_idx + 1) % len(cycle)]
            if next_target is None:
                self.exit_subagent_view()
            else:
                self.enter_subagent_view(next_target)
            return

        # Background-process detail toggle — separate key from shift+tab
        # above on purpose: bgprocs and subagents are different resources,
        # and cc-oss-style TUIs don't overload one shortcut across both.
        # Checked as two branches, not one shared condition, because once
        # the detail view is open, focus has moved off self.prompt onto
        # bgproc_detail_picker itself (see _show_bgproc_detail) — a single
        # "focus is prompt" gate would only ever open it, never close it
        # back with the same key.
        if key_id == "ctrl+b" and self._bgproc_viewing:
            self._hide_bgproc_detail()
            return
        if (
            key_id == "ctrl+b"
            and self.get_focus() is self.prompt
            and live_picker is None
        ):
            self._show_bgproc_detail()
            return

        if live_picker is not None:
            if key_id in ("up", "down"):
                live_picker.handle_key(key_id)
                self.request_render()
                return
            if key_id == "enter":
                live_picker.confirm()
                self.request_render()
                return
            # Tab deliberately falls through to the focused prompt below,
            # NOT live_picker.confirm() — Tab completes the text (via
            # PromptInput's own suggestion mechanism) without submitting,
            # so you can see/edit it first; only Enter submits. Matches
            # the old app: Tab handling lived entirely in PromptInput's
            # own suggestion check, never in the picker-interception
            # branch (which only ever listed up/down/enter).

        focused = self.get_focus()
        if focused is not None and hasattr(focused, "handle_key"):
            if focused.handle_key(key_id):
                # request_render() is what actually repaints — needed for
                # EVERY handled key, not just the newline tricks below.
                # Mutating keys (typing, backspace, ...) happen to also
                # trigger a render via on_change -> _update_slash_picker/
                # _update_at_picker -> _open_picker/_close_picker, but pure
                # cursor motion (arrows, alt+left/right, super+left/right)
                # never calls _changed(), so without this the cursor just
                # silently stopped moving on screen until some unrelated
                # periodic tick happened to mark the app dirty again.
                self.request_render()
                # Newline tricks (shift+enter/alt+enter/ctrl+j/\+enter) flip
                # the "used_multiline" flag mark_multiline_used() persists,
                # which is what lets newline_hint_text() go None and hand
                # the hint slot over to get_random_hint()'s rotation — only
                # re-derive the (randomly rolled) hint text for those keys,
                # not on every keystroke (that reroll-on-every-key was a
                # separate bug: see the request_render() split above).
                if key_id in ("shift+enter", "alt+enter", "ctrl+j", "enter"):
                    self._static_hint_text()

    def _on_terminal_resize(self) -> None:
        # Fires from ProcessTerminal's SIGWINCH handler — request_render()
        # just flips a bool, safe to call from a signal handler (Python
        # only runs signal handlers on the main thread, between bytecode
        # instructions).
        self.request_render()

    # ============================================= pickers

    def _bottom_bar_height(self) -> int:
        """Live row-count of bottom_bar's actual current content (working/
        ask-question text, both HRules, the prompt, statusbar, hint bar,
        subagent/bgproc status) — same measurement approach
        _ask_picker_layout uses, reused here so a bottom-anchored
        overlay can dodge it (see _open_picker)."""
        width, _ = self.tui._terminal_size()
        return len(self.bottom_bar.render(width))

    def _open_picker(self, picker, handle_attr: str, steal_focus: bool = True, anchor: str = "bottom-center", **overlay_kw) -> None:
        """Show `picker` as an overlay, closing whatever was already there
        under `handle_attr` first (pickers open one at a time in the
        original UI too — only one of model/rewind/dangerous/slash/at/
        login is ever visible). steal_focus=False is for slash_picker/
        at_picker: those filter live while the prompt keeps typing, so
        focus must stay on self.prompt — see _on_terminal_input's
        live_picker routing for how their up/down/enter/tab still reach
        them without owning focus.

        anchor defaults to "bottom-center" — every picker (model/rewind/
        dangerous/login/slash/at) uses it, and proximity to the prompt
        matters most for slash/at (you're actively typing into it).
        "bottom-center" alone lands flush against the very bottom of the
        terminal, directly on top of the working/ask_question_text/prompt/
        divider/statusbar/hint chrome that also lives there — that anchor
        has no idea those rows are occupied. offset_y nudges a bottom-
        anchored overlay up by bottom_bar's own live height (see
        _bottom_bar_height) so it lands just above that chrome instead of
        over it — computed fresh on every open rather than a fixed guess,
        so it can't drift out of sync with bottom_bar's actual current
        size (e.g. a wrapped multi-line prompt, or the subagent/bgproc
        status lines appearing).

        width="100%" (of the available area, minus margin) by default —
        every picker used to cap at min(80, avail_width) via
        OverlayOptions' own default, cramped on any real terminal width
        and the direct cause of ListPicker._render_item truncating long
        labels/descriptions down to one line with an ellipsis instead of
        actually showing them."""
        self._close_picker(handle_attr)
        offset_y = overlay_kw.pop("offset_y", 0)
        if anchor.startswith("bottom"):
            offset_y -= self._bottom_bar_height()
        handle = self.tui.show_overlay(
            picker, OverlayOptions(anchor=anchor, margin=1, width="100%", offset_y=offset_y, **overlay_kw)
        )
        setattr(self, handle_attr, handle)
        if steal_focus:
            self.set_focus(picker)
        self.request_render()

    def _close_picker(self, handle_attr: str) -> None:
        handle = getattr(self, handle_attr, None)
        if handle is not None:
            handle.hide()
            setattr(self, handle_attr, None)
        self.request_render()

    def close_all_pickers(self) -> bool:
        """Mirrors action_cancel_query's generic 'a picker is open, ESC
        just closes it' branch. Returns True if anything was actually
        closed (so the caller knows whether ESC's job is done)."""
        closed = False
        for handle_attr in (
            "_model_picker_overlay", "_rewind_picker_overlay", "_dangerous_picker_overlay",
            "_slash_picker_overlay", "_at_picker_overlay", "_login_picker_overlay",
        ):
            if getattr(self, handle_attr, None) is not None:
                self._close_picker(handle_attr)
                closed = True
        if closed:
            self.set_focus(self.prompt)
        return closed

    # --- picker selection callbacks -------------------------------------
    # Replaces on_option_list_option_selected/on_selection_list_selected_
    # changed's central id-dispatch — each picker gets its own callback,
    # wired once above (see the translation table's on_select/on_confirm
    # row for why).

    def _on_model_picked(self, item: PickerItem) -> None:
        self._current_model = item.value
        settings_store_.edit_setting("model", item.value)
        endpoint = get_endpoint()
        if endpoint != "Ollama":
            # OpenRouter aliases are free-text and never in MODELS, so
            # omitting backend here falls through to DEFAULT_TRIM_BUDGET
            # (100k) instead of BACKEND_DEFAULT_TRIM_BUDGET["openrouter"]
            # (1M) — silently shrinking history 10x on every re-pick.
            backend = "openrouter" if endpoint == "OpenRouter" else None
            self._trim_budget = model_registry.trim_budget_for(self._current_model, backend=backend)
        self._close_picker("_model_picker_overlay")
        self._refresh_status()
        self.set_focus(self.prompt)

    def _on_theme_picked(self, item: PickerItem) -> None:
        """Apply a preset. theme_store_.set_preset persists the color set and
        fires the on_change listeners — _apply_theme is the one registered
        here, which re-emits the terminal's default bg/fg and invalidates the
        render caches so every row repaints in the new set."""
        self._close_picker("_theme_picker_overlay")
        msg = theme_store_.set_preset(item.value)
        self._flash_status(msg, seconds=3)
        self.set_focus(self.prompt)

    def _on_subagents_picked(self, item: PickerItem) -> None:
        """Persist the concurrent-subagent cap. Read fresh from settings by
        every spawning microcc-headless process, so it applies to the next
        spawn — already-running subagents are never stopped by lowering it."""
        self._close_picker("_subagents_picker_overlay")
        settings_store_.edit_setting("max_subagents", int(item.value))
        self._flash_status(f"✓ up to {item.value} subagent(s) at once — applies to the next spawn", seconds=3)
        self.set_focus(self.prompt)

    def _apply_theme(self, _name: str = "") -> None:
        """Repaint an already-running UI after the color set changed.

        Two halves, and both are needed: (1) the terminal's own default
        bg/fg must be re-emitted, because OSC 11/10 only ran once at
        alt-screen entry — without this the ground stays whatever it was;
        (2) every cached render must be dropped, because widgets cache their
        flattened lines per width (message_row_.RichStatic/MessageRow,
        prompt_input_, the pickers) and would otherwise keep serving lines
        painted in the old palette until something else invalidated them.
        """
        from micro_cc.tui_native.alt_screen_ import default_colors_sequence

        sys.stdout.write(default_colors_sequence())
        sys.stdout.flush()

        # The splash is a RichStatic built from a theme-derived string, so it
        # has to be rebuilt, not just invalidated — invalidate() would repaint
        # the SAME stale markup in the old palette. banner() also hands back
        # the hue it chose, which the hint bar tints to match.
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

    def _on_rewind_picked(self, item: PickerItem) -> None:
        self._close_picker("_rewind_picker_overlay")
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

    def _on_dangerous_confirmed(self, checked: list) -> None:
        self._dangerous_tools = {it.value for it in checked}
        settings_store_.edit_setting("dangerous", list(self._dangerous_tools))
        self._close_picker("_dangerous_picker_overlay")
        self._refresh_status()
        self.set_focus(self.prompt)

    def _on_slash_picked(self, item: PickerItem) -> None:
        self._close_picker("_slash_picker_overlay")
        self.prompt.clear()
        self.prompt.insert(item.value)
        self.set_focus(self.prompt)
        self._safe_task(self._on_prompt_submitted(self.prompt.text), "prompt submit")

    def _on_login_picked(self, item: PickerItem) -> None:
        provider = item.value
        self._close_picker("_login_picker_overlay")
        if provider == "Bad Bunny":
            # No fields to walk (unlike every other LOGIN_SEQUENCE entry)
            # — runs as its own task instead of the _login_stage machinery.
            self.set_focus(self.prompt)
            self._safe_task(self._claude_code_login(), "OAuth login")
            return
        self._login_provider = provider
        self._login_stage = self.LOGIN_SEQUENCE[provider][0]
        self._input_mode = "login"
        self.prompt.placeholder = self.LOGIN_PROMPTS[self._login_stage]
        self._refresh_status()
        self.set_focus(self.prompt)

    # ============================================= pickers

    # ============================================= logins

    async def _claude_code_login(self) -> None:
        """Handle authentication using credentials from ~/.claude."""
        self._input_mode = "query_active"
        self._oauth_cancel_event = threading.Event()
        try:
            token = discover_claude_code_token()
            if token:
                self._flash_status("found existing Bad Bunny login")
            else:
                self._flash_status("opening browser for Bad Bunny login...")
                token = await oauth_login_flow(cancel_event=self._oauth_cancel_event)
            apply_login({"ANTHROPIC_OAUTH_TOKEN": token})

            # Mirrors the Anthropic/Foundry/LiteLLM branch in _handle_login_input
            # — OAuth reaches the same Anthropic models as a pasted API key, so
            # the model picker should offer the same options, not whatever the
            # previous backend left behind.
            options = model_registry.options_for_backend("anthropic")
            self.model_picker.set_items([PickerItem(o, o) for o in options])
            if self._current_model not in options:
                self._current_model = model_registry.DEFAULT_MODEL
                settings_store_.edit_setting("model", self._current_model)
            self._trim_budget = model_registry.trim_budget_for(self._current_model)

            self._flash_status("Bad Bunny configured")
        except asyncio.CancelledError:
            self._oauth_cancel_event.set()
            self._flash_status("Bad Bunny login cancelled")
            raise
        except Exception as e:
            self._flash_status(
                f"Bad Bunny login failed: {e}"
            )
        finally:
            self._input_mode = "idle"
            self._refresh_status()

    def _handle_login_input(self, query: str) -> None:
        key_name = self._login_stage

        # OLLAMA_BASE_URL: get_endpoint()/has_configured_endpoint() key off
        # this var, so it must land in .env even when the user just hits
        # Enter for the default — an empty value would get dropped by
        # apply_login's blank-filter below and Ollama wouldn't be detected
        # as the provider on next launch.
        if self._login_provider == "Ollama" and key_name == "OLLAMA_BASE_URL":
            query = query.strip() or "http://localhost:11434/v1"

        self._login_values[key_name] = query

        if self._login_provider == "Ollama" and key_name == "OLLAMA_MODEL":
            model = query.strip()
            error = check_ollama(model, self._login_values.get("OLLAMA_BASE_URL"))
            if error:
                # Don't advance — bounce back to the same field so the user
                # can retype the tag instead of restarting /login.
                self._flash_status(error)
                self._login_values.pop(key_name, None)
                self.prompt.placeholder = self.LOGIN_PROMPTS[
                    "OLLAMA_MODEL"
                ]
                return

        if self._login_provider == "OpenRouter" and key_name == "OPENROUTER_MODEL":
            model = query.strip()
            error = check_openrouter(model)
            if error:
                self._flash_status(error)
                self._login_values.pop(key_name, None)
                self.prompt.placeholder = self.LOGIN_PROMPTS[
                    "OPENROUTER_MODEL"
                ]
                return

        sequence = self.LOGIN_SEQUENCE[self._login_provider]
        next_index = sequence.index(key_name) + 1

        if next_index < len(sequence):
            self._login_stage = sequence[next_index]
            self.prompt.placeholder = self.LOGIN_PROMPTS[
                self._login_stage
            ]
            # Otherwise a captured field (esp. a paste, which replaces the
            # field with an opaque ⟪paste:N|...⟫ marker while typing) gives
            # no confirmation it registered before the box goes blank again
            # for the next stage.
            self._flash_status(f"✓ {key_name} saved")
            return

        if self._login_provider == "Ollama":
            model = self._login_values.get("OLLAMA_MODEL", "").strip()
            # Instance attrs, not class attrs — this app instance already
            # exists, so there's no reason to mutate MicroApp itself (that
            # was only ever done pre-instantiation, in the old start_()).
            self._current_model = model
            # Mirrors the model-picker OptionList handler below — without
            # this, ~/.micro-cc/settings.json keeps whatever "model" was
            # last picked (e.g. a prior Anthropic session's alias), and the
            # statusline script reads settings.json directly, so it'd keep
            # showing the stale model even though .env/OLLAMA_MODEL is right.
            settings_store_.edit_setting("model", model)
            # Derive from what the user just typed for NUM_CTX/MAX_OUTPUT
            # (falling back to their defaults on a blank Enter) rather than
            # a fixed number — a fixed budget would overflow a small
            # OLLAMA_NUM_CTX. See compute_ollama_trim_budget.
            num_ctx_input = self._login_values.get("OLLAMA_NUM_CTX", "").strip()
            max_output_input = self._login_values.get("OLLAMA_MAX_OUTPUT", "").strip()
            self._trim_budget = _ollama_trim_budget(
                num_ctx=int(num_ctx_input) if num_ctx_input else None,
                max_output=int(max_output_input) if max_output_input else None,
            )
            self.model_picker.set_items([PickerItem(model, model)])
        elif self._login_provider == "OpenRouter":
            # No fixed alias table — free-text model, same as Ollama above.
            model = self._login_values.get("OPENROUTER_MODEL", "").strip()
            self._current_model = model
            settings_store_.edit_setting("model", model)
            self._trim_budget = model_registry.trim_budget_for(model, backend="openrouter")
            self.model_picker.set_items([PickerItem(model, model)])
        elif self._login_provider in ("Anthropic", "Foundry", "LiteLLM", "OpenAI"):
            # Anthropic-direct/Foundry can't reach the gpt-5.6-* aliases, and
            # OpenAI-direct can't reach the Claude family (see
            # models/registry.py docstring) — without this the picker kept
            # showing them post-login and picking one 400'd against the API.
            backend_key = {
                "Anthropic": "anthropic",
                "Foundry": "foundry",
                "LiteLLM": "litellm",
                "OpenAI": "openai",
            }[self._login_provider]
            options = model_registry.options_for_backend(backend_key)
            self.model_picker.set_items([PickerItem(o, o) for o in options])
            if self._current_model not in options:
                self._current_model = model_registry.DEFAULT_MODEL
                # Mirrors the Ollama branch above — without this,
                # settings.json keeps the stale pre-switch model (e.g. an
                # Ollama tag like "qwen3:...") and the next launch reads it
                # straight back via the class-level _current_model default,
                # sending that invalid alias to the new backend and erroring.
                settings_store_.edit_setting("model", self._current_model)
            self._trim_budget = model_registry.trim_budget_for(self._current_model)

        apply_login(
            {k: v for k, v in self._login_values.items() if v}
        )  # drop blanks (skipped SERPAPI)
        self._flash_status(f"{self._login_provider} configured")
        self._login_stage = None
        self._login_provider = None
        self._login_values = {}
        self._input_mode = "idle"
        self.prompt.placeholder = ""
        self._refresh_status()

    # ============================================= logins

    # ============================================= other

    def _handle_keys_input(self, query: str) -> None:

        if self._keys_stage == "name":
            name = query.strip()
            if not name:  # blank name ends the loop
                if self._keys_values:
                    apply_project_keys(self._project_dir, self._keys_values)
                    self._flash_status(
                        f"Stored {', '.join(self._keys_values)} in "
                        f"{self._project_dir}/.env",
                    )
                else:
                    self._flash_status("No keys stored")
                self._keys_stage = None
                self._keys_pending_name = None
                self._keys_values = {}
                self._input_mode = "idle"
                self.prompt.placeholder = ""
                return
            self._keys_pending_name = name
            self._keys_stage = "value"
            self.prompt.placeholder = f"Value for {name}"
            return

        # stage == "value"
        self._keys_values[self._keys_pending_name] = query
        self._keys_pending_name = None
        self._keys_stage = "name"
        self.prompt.placeholder = "Another env var name — blank to finish"

    def _startup_model_options(self):
        """Model-picker options at mount time, filtered to whatever backend
        get_endpoint() resolves to from the persisted .env — mirrors the
        filtering _handle_login_input applies right after a fresh /login,
        so a restart with an existing Anthropic-only key doesn't re-show
        the litellm-only gpt-5.6-* aliases."""
        endpoint = get_endpoint()
        if endpoint in ("Ollama", "OpenRouter"):
            return [self._current_model] if self._current_model else []
        backend_key = {"Anthropic": "anthropic", "Foundry": "foundry", "OpenAI": "openai"}.get(
            endpoint, "litellm"
        )
        return model_registry.options_for_backend(backend_key)

    async def _mount_row(self, msg: dict) -> MessageRow:
        row = MessageRow(msg)
        self._msg_rows.append(msg)
        self.message_list.add(row)
        self.request_render()
        # Same rule as start_live_.py's _mount_row: the user's own outgoing
        # message always snaps the view down; everything else respects
        # whatever ScrollView.following_end already is.
        self._scroll_to_bottom(force=msg.get("type") == "user")
        return row

    def _scroll_to_bottom(self, force: bool = False):
        """Follow-the-bottom, chat-app style.
        """
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

    # ============================================= user questions

    def _swap_into_prompt_slot(self, component) -> None:
        """Put `component` where self.prompt normally lives in bottom_bar
        — the actual old-app behavior (picker.display = True in the SAME
        widget slot the prompt occupied, per start_live_.py's
        _show_current_ask_question), not a floating overlay. Sizes to its
        own real content (no overlay max_height clamp), and sits between
        the same two HRules the prompt normally does."""
        if component is not self._active_prompt_slot_component:
            self.bottom_bar.replace(self._active_prompt_slot_component, component)
            self._active_prompt_slot_component = component
        self.set_focus(component)
        self.request_render()

    def _show_current_ask_question(self):
        q = self._ask_questions[self._ask_stage]

        # RichStatic.render() returns [] when its content is empty — that
        # IS "hidden" for this component, there's no separate .display
        # flag to set (see the translation table above).
        self.statusbar.update("")
        self.static_hintbar.update("")
        self.ask_question_text.update(q["question"])

        items = [
            PickerItem(o["label"], o["label"], o.get("description", ""))
            for o in q["options"]
        ]

        if q["options"]:
            picker = self.ask_user_select if q.get("multiSelect") else self.ask_user_picker
            picker.max_visible, picker.max_desc_lines = self._ask_picker_layout(items)
            picker.set_items(items)
            self._swap_into_prompt_slot(picker)
        else:
            # Freeform stage — no picker at all, question text already
            # shown above; the real prompt IS the answer box.
            self._swap_into_prompt_slot(self.prompt)
            self.prompt.placeholder = q["question"]

    _ASK_PICKER_MARGIN = 2  # border rows the picker itself adds

    def _ask_picker_layout(self, items: list[PickerItem]) -> tuple[int, int]:
        """How many options the ask-question picker can show, and how many
        lines each option's description may wrap onto, without its own
        bottom_bar entry (shrink=0, i.e. never shrinks — see the comment on
        root.add(self.banner, ...)) growing past the real terminal height.
        When that happened, alt_screen_.do_render's own overflow fallback
        ('keep last `height` rows') was the thing that actually ate the
        question text and the top of the panel.

        Previously the description wrap was capped at a fixed constant
        (list_picker_.MAX_DESC_LINES) regardless of how much room was
        actually available, which ellipsized hints on every terminal even
        when there was plenty of space left. Ask-question stages have at
        most 4 options (tool schema caps it there), so the common case is
        real vertical budget to spare — split it across the actual item
        count instead of guessing at a worst case up front.

        Every other entry sharing bottom_bar's slot is already showing its
        real current content by the time this runs, so measuring them
        directly (rather than guessing at their sizes) costs nothing and
        can't drift out of sync with them."""
        width, height = self.tui._terminal_size()
        reserved = (
            len(self.banner.render(width))
            + len(self.working.render(width))
            + len(self.ask_question_text.render(width))
            + len(self.memory_flash.render(width))
            + 2  # top_rule + bottom_rule
            + len(self.statusbar.render(width))
            + len(self.static_hintbar.render(width))
            + len(self.subagent_status.render(width))
            + len(self.bgproc_status.render(width))
            + self._ASK_PICKER_MARGIN
        )
        budget = max(1, height - reserved)
        num_items = max(1, len(items))
        has_descriptions = any(it.description for it in items)
        if not has_descriptions:
            return max(1, budget), list_picker_.MAX_DESC_LINES

        if budget < num_items:
            # Too little room even for one line per item — fall back to
            # scrolling rather than stacking descriptions at all.
            return max(1, budget), 1

        max_desc_lines = max(1, budget // num_items - 1)  # -1 reserves the label line
        return num_items, max_desc_lines

    def _hide_ask_ui(self):
        self._swap_into_prompt_slot(self.prompt)
        self.ask_question_text.update("")
        self.prompt.placeholder = ""
        self._refresh_status()
        self._static_hint_text()

    def _advance_ask_user(self, answer):
        q = self._ask_questions[self._ask_stage]
        self._ask_answers[q["header"]] = answer
        self._ask_stage += 1
        if self._ask_stage >= len(self._ask_questions):
            self._pending_result = self._ask_answers
            self._pending_input.set()
        else:
            self._show_current_ask_question()

    # ============================================= user questions

    # ============================================= clipboard + paste monkey patching + double paste unravel full paste # TODO

    def on_paste(self, text: str) -> None:
        # Wired from _handle_input_item: a bracketed-paste block from
        # stdin_buffer_.StdinBuffer arrives as a Paste dataclass, unwrapped
        # to plain text before it gets here — always goes to the prompt
        # regardless of current focus, same as the old Textual on_paste's
        # "steal focus to the input box" behavior.
        if self.get_focus() is not self.prompt:
            self.set_focus(self.prompt)
        self.prompt._handle_paste(text)

    # ============================================= clipboard + paste monkey patching + double paste unravel full paste # TODO

    # ============================================= statusbar

    _STATIC_HINT_LINE_MAXLEN = 200
    # A long project_dir (deeply nested checkouts, /Users/<name>/... on macOS)
    # was eating the whole _STATIC_HINT_LINE_MAXLEN budget on its own, crowding
    # out the hint/update-available/corruption text that's supposed to follow
    # it on the same line — cap the path itself, independent of the line total.
    # 40 still wasn't tight enough: the actual constraint is the terminal's
    # real column width (RichStatic is single_line=True and gets clipped
    # there), not this 200-char budget, and the longest hint in hints.py is
    # ~70 chars on its own — on anything narrower than a very wide terminal,
    # a 40-char path left the hint truncated mid-sentence. 20 still keeps
    # the identifying tail (repo/subdir) readable via _shorten_path's "…"
    # prefix, just less of it.
    _PROJECT_DIR_DISPLAY_MAXLEN = 20

    @staticmethod
    def _shorten_path(path: str, maxlen: int) -> str:
        """Keeps the tail (the identifying part — .../repo/subdir) rather
        than the head, since two checkouts sharing a long prefix (e.g.
        /Users/<name>/code/...) are otherwise indistinguishable once cut."""
        if len(path) <= maxlen:
            return path
        return "…" + path[-(maxlen - 1):]

    # bgproc_status/subagent_status are the two genuinely multi-line (one
    # row per entry) status widgets in the bottom bar — unlike statusbar/
    # static_hintbar (single_line=True, hard-capped at 1 row by RichStatic
    # itself), nothing was capping how many rows THEY grow to. Enough
    # tracked subagents or background processes and this stack pushes the
    # prompt and message area off the bottom of a short terminal.
    _STATUS_STACK_MAX_LINES = 4

    @classmethod
    def _cap_lines(cls, lines: list[str]) -> list[str]:
        if len(lines) <= cls._STATUS_STACK_MAX_LINES:
            return lines
        shown = lines[: cls._STATUS_STACK_MAX_LINES - 1]
        return shown + [f"… +{len(lines) - len(shown)} more"]

    def get_random_hint(self):
        # Copy lives in utils/hints.py so the GUI shows the same tips; the ⌖
        # is this surface's own decoration.
        return "⌖ " + random.choice(hints.for_surface(hints.TUI))

    def _static_hint_text(self) -> None:
        # While a question owns the input, the status + hint bars are hidden
        # (_show_current_ask_question clears both). Several paths repaint them
        # unconditionally — most visibly shift+enter, which calls this to flip
        # the newline hint — and would resurrect the bar on top of the live
        # question. No-op here; _hide_ask_ui calls this again to restore it
        # once the question is answered.
        if self._input_mode == "question_asked":
            return
        text = f"⏣ {self._shorten_path(self._project_dir, self._PROJECT_DIR_DISPLAY_MAXLEN)}"
        if self._reload_pending and not self._restarting:
            # A self-change is queued but the app hasn't restarted yet (mid-turn
            # or a subagent still live) — say so, so the delay is legible rather
            # than looking like nothing happened.
            text += f" | [{theme_store_.get('accent')}]{self._self_reload_hint()}[/{theme_store_.get('accent')}]"
        elif self._update_available:
            text += f" | [{theme_store_.get('accent')}]⚠ micro-cc {self._update_available} available — run /update[/{theme_store_.get('accent')}]"
        elif settings_store_.was_reset_for_corruption():
            text += f" | [{theme_store_.get('error')}]⚠ ~/.micro-cc/settings.json was corrupt — reset to defaults[/{theme_store_.get('error')}]"
        else:
            # newline_hint_text() returns None once the user has actually
            # used a newline once (backslash+enter/shift+enter/ctrl+j) — no
            # separate hint line, just the top-priority slot in this one
            # until it's been learned, then it falls back to the rotation.
            # Tinted with the same colour as this launch's banner, so the
            # hint reads as "micro-cc talking" rather than dim status text.
            hint = newline_hint_text() or self.get_random_hint()
            hue = self._banner_hue
            text += f" | [{hue}]{hint}[/{hue}]"
        self.static_hintbar.update(
            text[: self._STATIC_HINT_LINE_MAXLEN]
        )
        self.request_render()

    # Same tier as ~/.micro-cc/.env, memory.json, settings.json. This file
    # IS the status bar — whatever it prints to stdout is used verbatim, no
    # schema, no curated context object. It gets one thing on stdin
    # (token_stats + project_dir) because those are live in this process
    # and exist nowhere on disk; everything else (model, dangerous tools,
    # memory entries) it's free to read itself from settings.json /
    # memory.json. First run writes the file below (old hardcoded behavior,
    # now just the seed content) so there's always something to edit.
    _STATUS_SCRIPT_PATH = os.path.expanduser("~/.micro-cc/statusline.sh")
    _STATUS_LINE_TIMEOUT = 0.3  # blocks the event loop briefly — keep tight
    _STATUS_LINE_MAXLEN = 200  # a runaway script can't blow up the bar
    from micro_cc.utils.default_status_sh import _DEFAULT_STATUS_SCRIPT

    def _ensure_status_script(self):
        if os.path.exists(self._STATUS_SCRIPT_PATH):
            return
        os.makedirs(os.path.dirname(self._STATUS_SCRIPT_PATH), exist_ok=True)
        with open(self._STATUS_SCRIPT_PATH, "w") as f:
            f.write(self._DEFAULT_STATUS_SCRIPT)
        os.chmod(self._STATUS_SCRIPT_PATH, 0o755)

    async def _status_text(self) -> str:
        self._ensure_status_script()
        import json
        payload = json.dumps(
            {"tokens": dict(token_stats), "project_dir": self._project_dir}
        ).encode()
        try:
            proc = await asyncio.create_subprocess_exec(
                self._STATUS_SCRIPT_PATH,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await asyncio.wait_for(
                proc.communicate(payload), timeout=self._STATUS_LINE_TIMEOUT
            )
            out = stdout.decode().strip()
            if out:
                return out[: self._STATUS_LINE_MAXLEN]
        except Exception:
            pass
        short_dir = self._shorten_path(self._project_dir, self._PROJECT_DIR_DISPLAY_MAXLEN)
        return f"⏣ {short_dir} | ◈ {self._current_model}"  # crash-safety only, not "the default"

    # Called at natural stopping points (loop finished, mount, a picker
    # closed, and — see etype_handler_.handle_turn_boundary/handle_final_text
    # — once per model-response round within a turn, not just once at the
    # very end) — never from the per-token streaming deltas, which is what
    # actually matters for cost: a round is an LLM round trip, always far
    # slower than this subprocess's own 0.3s timeout, so calls here can't
    # stack up faster than they resolve. No need to coalesce/debounce.
    def _refresh_status(self):
        # Was a bare asyncio.create_task — this is called constantly (once
        # per model-response round, every mount, every picker close), so
        # any bug inside _update_status_bar (like the checkpoint["as_of_index"]
        # KeyError this line was hardened against) surfaced as asyncio's
        # own "Task exception was never retrieved" raw traceback dump,
        # same corruption class as an unguarded print() — bypasses
        # do_render entirely, lands wherever the cursor happens to be.
        # _safe_task is this codebase's existing fix for exactly that
        # (see its docstring) — reuse it, don't invent a second mechanism.
        self._safe_task(self._update_status_bar(), "status bar update")

    async def _update_status_bar(self):
        # Same reason as _static_hint_text's guard: a question hides both
        # bottom bars, and the frequent refreshes (_refresh_status fires on
        # every mount/flash/poll) would otherwise repaint the status line over
        # the live question. _hide_ask_ui restores it when the question ends.
        if self._input_mode == "question_asked":
            return
        # load_checkpoint() returns None until compaction has ever run
        # once for this project — checkpoint["as_of_index"] on that None
        # was throwing on every single status refresh (i.e. constantly)
        # for any project that hadn't compacted yet. Also: `last_checkpoint`
        # was never declared anywhere — a bare local name read before any
        # assignment reaches it, and even the write below it would only
        # ever create a fresh local each call, never actually persisting
        # between calls. self._last_checkpoint_index (in __init__) is the
        # real persistent value to diff against.
        checkpoint = load_checkpoint(self._project_dir)
        if checkpoint is not None and checkpoint["as_of_index"] != self._last_checkpoint_index:
            self._last_checkpoint_index = checkpoint["as_of_index"]
            # Must return here — falling through to the normal status text
            # below would overwrite the flash in this same call, before it
            # ever got a chance to render. _flash_status's own timer is what
            # brings the normal text back after `seconds`.
            self._flash_status(
                f"[bold {theme_store_.get('warn')}]✂ compacted past conversation just now[/bold {theme_store_.get('warn')}]",
                seconds=8,
            )
            return

        text = await self._status_text()
        self.statusbar.update(text)
        self.request_render()

    def _flash_status(self, text: str, seconds: float = 3.0):
        """Show a transient message in the status bar, then restore it."""
        self.statusbar.update(text)
        self.request_render()
        if self._flash_status_timer_task is not None:
            self._flash_status_timer_task.cancel()
        self._flash_status_timer_task = self._call_later(seconds, self._refresh_status)

    def _disarm_esc_clear(self) -> None:
        """Timeout callback for the Esc-Esc-clears-prompt arming in the Esc
        handler (_ESC_CLEAR_TIMEOUT after the first Esc). The status hint
        ("Esc again to clear") already self-clears via _flash_status's own
        timer — this only needs to drop the arm flag so a stray Esc after
        the window closes doesn't wrongly clear the prompt."""
        self._esc_clear_armed = False

    # ---------------------------------------------------- restart on self-change
    # The harness is plain .py files loaded from a source tree, so when its own
    # code changes on disk the running process is executing stale code objects
    # — a fresh process is the only honest way to pick the change up (see
    # utils/self_reload_'s module docstring for why importlib.reload can't do
    # it). This is the TUI half: detect the change, then relaunch at a turn
    # boundary. The relaunch goes through the supervisor (micro_cc.self_heal_),
    # so a self-edit that breaks the tree is caught and rolled back to the
    # published wheel rather than leaving the user with an app that won't start.

    def _init_self_manifest(self) -> None:
        """Snapshot the package's own source once at startup.

        Runs on EVERY install — a dev checkout, pyenv/brew/pipx/uv, a
        site-packages copy inside WSL2. Where the package physically lives is
        irrelevant: the harness knows its own package dir (claude_loop_ prints
        it as "your own source code lives at"), venv and pipx trees are
        writable, so a normal install is just as self-editable as a checkout.

        This used to gate on is_source_checkout() — a "site-packages not in
        path" string test. That silently disabled the whole feature on every
        user's install (the poll watched nothing, so no self-edit was ever
        detected), and the test itself is unreliable anyway (nothing guarantees
        the segment is spelled "site-packages" on every manager/platform).
        The source-vs-installed question is only meaningful for self_update_'s
        "clobber the tree?" decision, never for watching it."""
        from micro_cc.utils import self_reload_

        self._self_manifest = self_reload_.build_manifest()

    async def _self_poll_loop(self) -> None:
        while True:
            await asyncio.sleep(2.0)
            # A bare tick with no guard is the nastiest failure mode of this
            # whole feature: one exception (a race on a half-written file, a
            # transient OSError stat'ing the tree) kills the loop FOREVER, and
            # the symptom is exactly "hint shows, nothing ever restarts, no
            # error anywhere" — the poll simply stops ticking. Swallow and
            # keep the loop alive; the next tick retries.
            try:
                self._poll_self_change_tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                pass

    def _poll_self_change_tick(self) -> None:
        if self._self_manifest is None or self._restarting:
            return
        from micro_cc.utils import self_reload_

        current = self_reload_.build_manifest()
        changed = self_reload_.diff_manifest(self._self_manifest, current)
        if changed:
            self._self_manifest = current
            self._reload_changed = changed
            self._reload_pending = True
            self._static_hint_text()  # show the pending hint (hintbar, not statusbar)
            # If a gate is holding the restart, say why ONCE per detected
            # change — not every tick (that would flash the status bar twice a
            # second for as long as the gate lasts). The hint bar also carries
            # the blocker, but a transient flash is what the user actually
            # notices; one per edit is signal, one per tick is noise.
            blocker = self._self_reload_blocker()
            if blocker:
                self._flash_status(f"↻ reload queued — {blocker} active", seconds=4)

        # Drain on EVERY tick while a reload is pending, not only on the tick
        # that detected the change. The detect-and-drain used to be one atomic
        # step, which wedged: the tick that noticed the edit ran mid-turn (so
        # the drain deferred, correctly), but it had ALREADY advanced the
        # manifest — so every later tick diffed clean, returned early, and the
        # drain was never retried. Result: hint shown, app idle, nothing fires,
        # forever. Retrying here makes the pending state self-healing: the next
        # tick after the turn ends (or whatever gate was holding it clears)
        # actually restarts.
        #
        # The cheap idle pre-check avoids spawning a task on every tick during
        # a long streaming turn (where the answer is always "not yet" anyway);
        # _maybe_self_reload still re-checks every gate, this is just to keep
        # the common busy case from churning tasks.
        if self._reload_pending and self._input_mode == "idle":
            self._safe_task(self._maybe_self_reload(), "self reload")

    def _self_reload_hint(self) -> str:
        """Short by design — this shares one truncated line with the project
        path, so every extra word costs the user a word they don't get to
        read. One file: name it. Many: just the count. Name the blocker only
        when there is one, and keep it to a word: the whole point is that the
        reload stall is legible at a glance without crowding out the path."""
        n = len(self._reload_changed)
        if n == 0:
            base = "↻ reloading…"
        elif n == 1:
            base = f"↻ {os.path.basename(next(iter(self._reload_changed)))} — reloading…"
        else:
            base = f"↻ {n} files — reloading…"
        # A pending reload with no visible reason is indistinguishable from a
        # wedged one (the exact confusion this feature already cost a session)
        # — name the gate holding us, so a stall reports itself.
        blocker = self._self_reload_blocker()
        if blocker:
            base += f" ({blocker})"
        return base

    def _self_reload_blocker(self) -> str | None:
        """Why _maybe_self_reload won't restart right now, or None if it would.
        Mirrors that method's gates in order — keep the two in sync."""
        if self._restarting:
            return None
        if self._input_mode != "idle":
            return self._input_mode.replace("_", " ")
        current = self._current_query_task
        if current is not None and not current.done():
            return "turn"
        if getattr(self, "_gui_shutdown", None) is not None:
            return "gui"
        if _pending_subagents(self._project_dir):
            return "subagent"
        return None

    async def _maybe_self_reload(self) -> None:
        """Drain a pending self-reload IF it's safe to restart right now.
        Called at the same turn-boundary points that already gate on the app
        being idle (see do_query's finally, _drain_next_queued). Never fires
        mid-turn: a restart there would kill an in-flight tool call and, worse,
        re-exec while a subagent or the /gui server still owns state."""
        if not self._reload_pending or self._restarting:
            return
        # Same gates /exit and /update respect — nothing in flight, no browser
        # owning the conversation, no live subagent we'd orphan.
        if self._input_mode != "idle":
            return
        # A live turn blocks the restart — but NOT this task when we're being
        # called from the tail of that very turn's own finally (do_query sets
        # _input_mode="idle" then calls this): at that point the turn is over,
        # and _current_query_task is still "not done" only because we're
        # executing inside it. Excluding the current task lets a turn-boundary
        # reload fire immediately instead of waiting for the next poll tick; a
        # genuinely concurrent turn (a different task) still blocks it.
        current = self._current_query_task
        if current is not None and not current.done() and current is not asyncio.current_task():
            return
        if getattr(self, "_gui_shutdown", None) is not None:
            return
        if _pending_subagents(self._project_dir):
            return
        await self._restart_self()

    async def _restart_self(self, reason: str | None = None) -> None:
        """Relaunch into the changed code. Shared by the auto-reload path above
        and the explicit /reload command (screen_cmds_.cmd_reload).

        Order matters and mirrors _run_update: flash, stash the unsent prompt,
        tear the terminal down (stop() restores termios/alt-screen/Kitty and
        kills monitor watches), flush the conversation, then execv. The new
        process rebuilds everything from disk — messages.jsonl is the durable
        boundary, so the conversation keeps its place across the restart.
        """
        if self._restarting:
            return
        self._restarting = True

        from micro_cc.utils import self_reload_

        self_reload_.save_pending_prompt(self._project_dir, self.prompt.text)

        # Reuse _self_reload_hint so the "reloading" text has ONE definition —
        # this used to be a second, longer copy that could (and did) drift out
        # of sync with the pending hint shown in the bar.
        msg = reason or self._self_reload_hint()
        self.static_hintbar.update(f"[{theme_store_.get('accent')}]{msg}[/{theme_store_.get('accent')}]")
        self.request_render()
        try:
            # Let the frame land before the screen tears down. Cancellation
            # (Esc/Ctrl+C) can arrive in this window — if it does, undo the
            # _restarting claim so the app stays up AND can still retry at the
            # next turn boundary, rather than being wedged with a pending
            # reload it will never run.
            await asyncio.sleep(0.6)
        except asyncio.CancelledError:
            self._restarting = False
            raise

        # Flush whatever the loop has committed so far — the relaunched process
        # reads this back and mounts it as history.
        if self._loop_msgs is not None:
            store_msgs(self._project_dir, self._loop_msgs)

        await self.stop()
        # stop() cleared _running and tore the terminal down; execv replaces
        # this image with a fresh interpreter running the supervisor.
        self_reload_.exec_relaunch()

    def _flash_memory_review(self, text: str, seconds: float = 6.0) -> None:
        """Same shape as _flash_status, but its own widget (self.memory_flash)
        and its own timer (self._memory_flash_timer_task) — see
        _poll_memory_review_tick. Deliberately not routed through
        _flash_status: a memory review runs right after a compaction fold
        finishes (msg_store_.compact_checkpoint fires _review_memory
        immediately after store_checkpoint), so the compaction flash and
        this one can legitimately both be in flight close together.
        _flash_status has exactly one shared target/timer — reusing it here
        would let whichever fires second cancel and stomp the other's
        still-pending message before its own timeout ever runs out.

        "Restore" here is just clearing the line, not recomputing real
        status text — this widget has no persistent content of its own
        between flashes (see _build_widget_tree: empty renders as zero
        rows), unlike statusbar's own restore path."""
        self.memory_flash.update(text)
        self.request_render()
        if self._memory_flash_timer_task is not None:
            self._memory_flash_timer_task.cancel()
        self._memory_flash_timer_task = self._call_later(seconds, self._clear_memory_flash)

    def _clear_memory_flash(self) -> None:
        self.memory_flash.update("")
        self.request_render()

    def copy_to_clipboard(self, text: str) -> None:
        """Was never ported off MicroApp — cmd_copy/cmd_gui both call this
        and it simply didn't exist, which is why /gui crashed with
        AttributeError. pyperclip first (what actually works on a normal
        host), OSC 52 as a best-effort fallback that can reach a real
        terminal over SSH — no super().copy_to_clipboard() to fall back to
        here, that was Textual's own OSC 52 implementation."""
        landed = False
        pyperclip_error = None
        try:
            import pyperclip
            pyperclip.copy(text)
            landed = True
        except Exception as e:
            # See alt_screen_.py's copy_selection_to_clipboard for why the
            # real exception goes in the flash rather than a log file.
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
        """Signal 'working' to the terminal itself via OSC 9;4 progress.
        state 3 = indeterminate (iTerm2/Ghostty/WezTerm/Win-Terminal render a
        busy indicator on the tab), 0 = clear. It's an out-of-band escape (no
        cursor movement), so writing straight to the tty is safe even while
        Textual owns the alt-screen. Unsupported terminals just ignore it."""
        seq = "\033]9;4;3;0\a" if on else "\033]9;4;0;0\a"
        try:
            with open("/dev/tty", "w") as tty:
                tty.write(seq)
                tty.flush()
        except OSError:
            pass

    def _alert_attention(self):
        """Ring the terminal bell once. Approval/question prompts block the
        turn on user input, but _set_busy's OSC 9;4 state stays "busy" the
        whole time they're waiting (do_query's span covers the nested wait
        too) — indistinguishable from ordinary in-progress work if you've
        tabbed away. A bell is the one signal most terminals still surface
        out-of-band (tab flash, dock bounce, audible beep) regardless of
        whether OSC 9;4 is supported."""
        try:
            with open("/dev/tty", "w") as tty:
                tty.write("\a")
                tty.flush()
        except OSError:
            pass

    def _set_busy(self, on: bool):
        """Toggle the whole-turn working state: terminal signal + the in-app
        nudge above the input. RichStatic has no .display flag — .update("")
        alone hides it (empty content renders zero lines, see _tick_working
        below and message_row_.RichStatic.render())."""
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
            await asyncio.sleep(0.4)
            self._tick_working()

    def _tick_working(self):
        """Spinner frame + the active task's activeForm (set by todo_tool_),
        polled from state_store so it tracks whatever Claude is currently doing."""
        store = state_store.get_todos(self._project_dir) or {}
        active = next(
            (t for t in store.values() if t.get("status") == "in_progress"), None
        )
        label = (
            (active.get("activeForm") or active.get("content")) if active else "Working"
        )
        frame = self._SPINNER[self._spin_i % len(self._SPINNER)]
        self._spin_i += 1
        self.working.update(f"{frame} {label}…")
        self.request_render()

    # ============================================= terminal busy and status of terminal logic

    # ============================================= loading /reloadfing messages

    REWIND_LIMIT = 50

    def get_rewind_options(self, messages):
        """User messages for the rewind picker, oldest-first.

        Returns (index, label) pairs — index is the position in `messages`,
        which is load_msgs() order, i.e. exactly what rewind_msgs() cuts on.
        The label is display-only; nothing matches on it.
        """
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
        """Re-render the message list from messages.jsonl, discarding whatever
        is currently mounted.

        Needed anywhere another writer may have appended to the file behind
        this app's back — e.g. the /gui browser tab, which runs its own
        session against the same project and calls store_msgs() directly.
        Same clear-and-remount shape as the rewind-picker handler.
        """
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
        """Reload from disk, THEN drain anything that queued up while /gui
        owned the conversation (an incoming message_session_ delivery or a
        subagent wakeup — see _inject_incoming_turn's "gui" branch). Must be
        sequenced, not two separate workers: reload's remove_children()/
        mount_all would otherwise race a concurrent drain trying to
        remove/mount rows of its own."""
        await self._reload_messages_from_disk()
        await self._drain_next_queued()

    # ============================================= loading /reloadfing messages

    # ============================================= prompt mechanics files slash pickers

    def _on_prompt_changed(self, text: str) -> None:
        """Replaces the dead Textual on_text_area_changed message — wired
        directly as PromptInput.on_change in _build_widget_tree, called
        from PromptInput._changed() after every edit."""
        self._update_slash_picker(text)
        self._update_at_picker()

    def _update_slash_picker(self, text: str):
        """Show a filtered command panel the moment '/' is typed; hide it
        otherwise. Selection itself is handled by _on_terminal_input
        routing up/down/enter to self.slash_picker.handle_key while it's
        focused, plus the on_select callback wired in _build_widget_tree
        (click, once mouse-to-component dispatch exists — today's mouse
        handling only covers drag-select, see TuiAltScreen.
        handle_selection_mouse_event)."""
        matches = []
        if text.startswith("/") and "\n" not in text:
            matches = [
                (c, d) for c, d in PromptInput.SLASH_COMMAND_INFO if c.startswith(text)
            ]
        if not matches:
            self._close_picker("_slash_picker_overlay")
            return
        self.slash_picker.set_items([
            PickerItem(cmd, f"{cmd:<12}{desc}") for cmd, desc in matches
        ])
        self._open_picker(self.slash_picker, "_slash_picker_overlay", steal_focus=False)

    _AT_MENTION_MAX_RESULTS = 50

    def _current_mention_query(self) -> str | None:
        """Text after the nearest unspaced '@' on the current line, up to the
        cursor — None if the cursor isn't sitting inside such a fragment."""
        row, col = self.prompt.cursor_location
        line = self.prompt.get_line(row)[:col]
        at_idx = line.rfind("@")
        if at_idx == -1:
            return None
        fragment = line[at_idx + 1:]
        if any(c.isspace() for c in fragment):
            return None
        return fragment

    def _update_at_picker(self):
        """Mirrors the GUI's @ dropdown (multimodal-input.tsx): typing '@'
        opens a fuzzy filter over the project's files."""
        if self._slash_picker_overlay is not None:
            self._close_picker("_at_picker_overlay")
            return
        query = self._current_mention_query()
        matches = []
        if query is not None:
            matches = rank_file_matches(
                self._project_files, query, self._AT_MENTION_MAX_RESULTS
            )
        if not matches:
            self._close_picker("_at_picker_overlay")
            return
        self.at_picker.set_items([PickerItem(f, f) for f in matches])
        self._open_picker(self.at_picker, "_at_picker_overlay", steal_focus=False)

    def insert_at_mention(self, file_path: str):
        """Replace the '@query' fragment under the cursor with file_path."""
        row, col = self.prompt.cursor_location
        line = self.prompt.get_line(row)
        at_idx = line.rfind("@", 0, col)
        if at_idx != -1:
            self.prompt.replace(file_path, (row, at_idx), (row, col))
        self._close_picker("_at_picker_overlay")
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
        # Checked first, ahead of every _input_mode branch below — the
        # bgproc detail view (ctrl+b) is deliberately orthogonal to
        # _input_mode (see _bgproc_viewing's own comment in __init__), so
        # it can't be reached by keying off _input_mode the way gui/login/
        # keys/question_asked are below. Esc here is just "close this
        # peek," never "cancel the turn."
        if self._bgproc_viewing:
            self._hide_bgproc_detail()
            return

        # /gui handed the session to the browser — ESC takes it back. First,
        # because while the GUI owns the conversation there's nothing else in
        # this app for ESC to mean.
        if self._input_mode == "gui":
            from micro_cc.tui_native.screen_cmds_ import stop_gui

            if stop_gui(self):
                self._safe_task(self._resume_after_gui(), "resume after gui")
                self._flash_status("⇤ GUI stopped — terminal is live again")
            return

        if self._input_mode == "login":
            self._close_picker("_login_picker_overlay")
            self._login_stage = None
            self._login_provider = None
            self._login_values = {}
            self._input_mode = "idle"
            self.prompt.placeholder = ""
            self.set_focus(self.prompt)
            return

        # /keys — same bail-out shape as /login above, no picker to close
        if self._input_mode == "keys":
            self._keys_stage = None
            self._keys_pending_name = None
            self._keys_values = {}
            self._input_mode = "idle"
            self.prompt.placeholder = ""
            self.set_focus(self.prompt)
            return

        # If a picker is open, ESC just closes it — no cancel
        if self.close_all_pickers():
            return

        # Mid-approval — the row literally says "esc to reject", but this
        # used to fall through to workers.cancel_all() and kill the whole
        # turn. Reject just this tool and let the loop carry on; a second esc
        # (now back in query_active) is what interrupts.
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

        # Idle, no picker open, nothing pending — there's no query to
        # interrupt. This used to fall straight through to the
        # workers.cancel_all()/" interrupted" block below, so a stray Esc
        # (habit, or dismissing a suggestion) permanently spammed a bogus
        # "interrupted" row into the transcript. Give it real, contained
        # meaning instead: Esc-Esc clears the prompt.
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

        # The actual fix for "ESC doesn't stop the stream" — nothing else
        # ever cancels this task. Cancelling here is what makes the
        # `async for event in claude_loop(...)` loop in do_query actually
        # stop; everything below is the same immediate, synchronous UI
        # cleanup the old Textual workers.cancel_all() path did while the
        # cancellation was still propagating.
        if self._current_query_task is not None and not self._current_query_task.done():
            self._current_query_task.cancel()

        if self._pending_input is not None:
            self._pending_input.set()
        self._pending_input = None
        self._pending_result = None
        self._input_mode = "idle"
        if self._loop_msgs is not None:
            # Capture partial streaming text from UI into API msgs
            if self._streaming is not None:
                partial = self._streaming.get_content().strip()
                if partial:
                    self._loop_msgs.append({"role": "assistant", "content": partial})
            self._finalize_streaming()
            store_msgs(self._project_dir, self._loop_msgs)
        err = {"type": "error", "content": " interrupted"}
        self._msg_rows.append(err)
        self.message_list.add(MessageRow(err))
        self._scroll_to_bottom()

        ## clean queue
        for _, row, msg, _ in self._queue:
            self.message_list.remove(row)
            if msg in self._msg_rows:
                self._msg_rows.remove(msg)
        self._queue.clear()

        self._set_busy(False)
        self.set_focus(self.prompt)

    # ============================================= ESC

    # ============================================= do query

    async def _on_prompt_submitted(self, text: str) -> None:
        """Replaces on_prompt_input_submitted — wired as
        `self.prompt.on_submit` in _build_widget_tree (wrapped in a task
        there since handle_key("enter") calls it synchronously)."""
        query = text.strip()
        query = re.sub(
            r'⟪paste:(\d+)\|\d+ chars, \d+ lines⟫',
            lambda m: self.prompt._paste_store.get(int(m.group(1)), m.group(0)),
            query,
        )
        self.prompt._paste_store.clear()
        self.prompt._paste_id = 0

        # The browser owns the conversation while /gui is up — swallow
        # input here rather than letting two surfaces append to the same
        # jsonl.
        if self._input_mode == "gui":
            self.prompt.clear()
            self._flash_status(f"▸ GUI has the session — {self._gui_url} · esc to stop")
            return

        if self._input_mode == "login":
            self._handle_login_input(query)
            self.prompt.clear()
            return

        if self._input_mode == "keys":
            self._handle_keys_input(query)
            self.prompt.clear()
            return

        if self._input_mode == "question_asked":
            if self._pending_input is not None and not self._pending_input.is_set():
                self._advance_ask_user(query)
            self.prompt.clear()
            return

        if self._input_mode == "awaiting_approval":
            if self._pending_input is not None and not self._pending_input.is_set():
                self._pending_result = query.lower() in ("", "y", "yes")
                self._pending_input.set()
            self.prompt.clear()
            return

        if not query:
            return

        # Prefix commands (e.g. "/setup", "/new-skill do X") — take free
        # text after the command name.
        lower_q = query.lower()
        matched_prefix = next(
            (p for p in PREFIX_COMMANDS if lower_q == p or lower_q.startswith(p + " ")),
            None,
        )
        handler = None if matched_prefix is not None else SLASH_COMMANDS.get(lower_q)

        # Gate commands on an active stream — /clear, /model, /rewind etc.
        # mutate the same message_list/_loop_msgs state etype_handler_ is
        # concurrently writing into for the in-flight turn; running one
        # mid-stream is a race, not just a UX oddity (e.g. /clear wiping
        # rows a handler is about to mount into). Esc still interrupts
        # first if the user wants to run one right now. Text stays in the
        # prompt so they can just hit enter again once it's done.
        if (matched_prefix is not None or handler is not None) and self._input_mode == "query_active":
            self._flash_status(
                "⏳ still streaming — esc to interrupt, then retry the command", seconds=4
            )
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

        # Image attachment — dropped/typed image path(s) are stripped out
        # of the query text and encoded now, so they ride this SAME turn
        # as real content blocks instead of a separate tool round trip.
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

        if not has_configured_endpoint():
            err = {"type": "error", "content": "No API endpoint configured — run /login"}
            await self._mount_row(err)
            return

        # Ollama has no persistent server-side session to go stale, but the
        # daemon can die or the pulled tag can get `ollama rm`'d out from
        # under a working config between logins.
        if get_endpoint() == "Ollama":
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
                self._login_provider = "Ollama"
                self._login_stage = self.LOGIN_SEQUENCE["Ollama"][0]
                self._login_values = {}
                self._input_mode = "login"
                self.prompt.placeholder = self.LOGIN_PROMPTS[self._login_stage]
                self.prompt.clear()
                self._refresh_status()
                return

        self._input_mode = "query_active"
        self._current_query_task = self._safe_task(self.do_query(query, encoded_image=encoded_image), "do_query")

    async def _drain_next_queued(self) -> bool:
        """Pop the next queued turn (if any) and start it. Shared by
        do_query's finally block (drains the instant a turn ends) and
        _resume_after_gui (drains once the TUI regains sole write access to
        messages.jsonl). Returns True if something was popped and started."""
        if not self._queue:
            return False
        # Re-claim BEFORE any await — a submit landing mid-mount would
        # otherwise see _input_mode="idle" and race a second worker.
        self._input_mode = "query_active"
        next_q, next_row, next_msg, next_image = self._queue.popleft()
        # The queued row was mounted mid-response, so later streaming/tool
        # rows sit BELOW it. Promoting in place would leave the user msg
        # stranded mid-transcript — remove it and re-mount at the end.
        # (It may already be gone — e.g. _resume_after_gui's reload wiped
        # it before this ran — removing an unmounted widget is a no-op we
        # don't need to treat as an error.)
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
                dangerous_tools=self._dangerous_tools,
                surface="tui",
                encoded_image=encoded_image,
            ):
                etype = event.get("type")
                etype_handler = ETYPES.get(etype)
                if etype_handler is not None:
                    await etype_handler(
                        self, event, container, tool_call_rows, approval_rows
                    )
        except asyncio.CancelledError:
            # action_cancel_query is the only thing that cancels this task
            # (see self._current_query_task), and it already does the
            # user-facing cleanup — mounts the "interrupted" row, finalizes
            # streaming, stores msgs — synchronously and immediately for
            # instant feedback, rather than waiting for cancellation to
            # actually propagate here. Doing all of that again in this
            # handler used to double it up (two "interrupted" rows). Just
            # let it fall through to `finally` below, which still needs to
            # run regardless (busy state, focus, status refresh, queue
            # drain) and is idempotent against work action_cancel_query
            # already did.
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
            # A queued turn starts a new turn right here — only consider a
            # self-reload once nothing else is waiting to run, so we never
            # restart out from under a queued prompt.
            if not await self._drain_next_queued():
                await self._maybe_self_reload()

    # ============================================= do query

    # ============================================= subagents

    async def _inject_incoming_turn(self, content: str) -> None:
        """Start a turn for `content` right now if idle, or queue it if busy
        — drained by handle_turn_boundary/do_query same as any queued
        prompt, or by _resume_after_gui if /gui was the reason. Shared by
        message_session_ deliveries (_handle_session_message) and subagent
        checkpoint wakeups (_wake_for_subagent) — both are "something
        external decided boss should see this now" events with identical
        queue-or-start semantics.

        "gui" counts as busy too: the browser tab is the sole writer to
        messages.jsonl while /gui is active (see cmd_gui's docstring) —
        starting a do_query turn from here at the same time would be a
        second writer racing it.
        """
        if self._input_mode in ("query_active", "gui"):
            msg = {"type": "user_queued", "content": content}
            row = await self._mount_row(msg)
            self._queue.append((content, row, msg, None))
            return
        await self._mount_row({"type": "user", "content": content})
        self._input_mode = "query_active"
        self._current_query_task = self._safe_task(self.do_query(content), "do_query")

    def _on_session_message(self, from_dir: str, text: str) -> None:
        """session_ipc_'s socket callback — fires on this app's own asyncio
        loop but outside any Textual worker, so hand off to a task rather
        than doing async work inline here."""
        self._safe_task(self._handle_session_message(from_dir, text), "incoming session message")

    def _on_monitor_event(self, content: str) -> None:
        """monitor_watch_'s wake_callback — a live watch just fired (or
        ended/auto-stopped) and wants boss to see it now, same "something
        external decided boss should see this now" shape as an incoming
        message_session_ call or a subagent checkpoint. Already fully
        formatted by the caller (monitor_watch_._fire/_finish), so this is
        just the same hand-off-to-a-task wrapper _on_session_message uses —
        the callback itself runs synchronously off a non-Textual-worker
        context (an asyncio reader/timer inside monitor_watch_)."""
        self._safe_task(self._inject_incoming_turn(content), "monitor watch event")

    async def _handle_session_message(self, from_dir: str, text: str) -> None:
        """Deliver an incoming message_session_ call the same way a typed
        prompt is handled."""
        await self._inject_incoming_turn(f"[incoming from {from_dir}]\n{text}")

    def _bgproc_picker_items(self, procs: list[dict], watches: list[dict] | None = None) -> list:
        """One PickerItem per backgrounded process or active monitor watch —
        label is the compact glyph+age already on the status line,
        description is the full command (+ cwd, for a process) that line no
        longer shows inline. Watch items are value-prefixed "watch:<id>" so
        they can never collide with a PID's own str(int) value (both are
        opaque strings to _refresh_bgproc_detail's selected-item tracking,
        which matches purely on this value). ListPicker's own renderer
        builds plain ANSI strings (see list_picker_.render/_render_item —
        no Text.from_markup anywhere in it), unlike RichStatic, so unlike
        bgproc_status's own line this needs no markup-escaping of the
        untrusted command/description text."""
        items = []
        for p in procs:
            desc = p["command"]
            if p.get("cwd"):
                desc += f"\ncwd: {p['cwd']}"
            items.append(PickerItem(str(p["pid"]), f"PID {p['pid']} · {p['age']}s", desc))
        for w in (watches or []):
            items.append(PickerItem(
                f"watch:{w['watch_id']}",
                f"◎ {w['watch_id']} · {w['age']}s",
                f"{w['description']}\ncommand: {w['command']}",
            ))
        return items

    def _show_bgproc_detail(self) -> None:
        """ctrl+b — swap the prompt slot for a read-only list of every
        currently backgrounded process AND active monitor_(action="watch")
        stream, full command text included (see _bgproc_picker_items).
        Mirrors _show_current_ask_question's own swap-into-prompt-slot
        mechanism (_swap_into_prompt_slot), just for a peek rather than a
        real question — nothing here blocks or answers anything, so
        _input_mode is left alone; _bgproc_viewing is its own orthogonal
        flag (see its own comment in __init__)."""
        from micro_cc.tools.bash_tool import list_background_processes
        from micro_cc.tools.monitor_watch_ import list_watches

        procs = list_background_processes()
        watches = list_watches()
        if not procs and not watches:
            self._flash_status("no background processes or watches", seconds=2.5)
            return
        items = self._bgproc_picker_items(procs, watches)
        self.bgproc_detail_picker.max_visible, self.bgproc_detail_picker.max_desc_lines = (
            self._ask_picker_layout(items)
        )
        self.bgproc_detail_picker.set_items(items)
        self._bgproc_viewing = True
        self._swap_into_prompt_slot(self.bgproc_detail_picker)

    def _hide_bgproc_detail(self) -> None:
        self._bgproc_viewing = False
        self._swap_into_prompt_slot(self.prompt)

    def _refresh_bgproc_detail(self, procs: list[dict], watches: list[dict] | None = None) -> None:
        """Keeps the open detail view's ages (and item count) live off the
        same 3s tick that already refreshes bgproc_status, rather than
        freezing at whatever the list looked like the moment ctrl+b was
        pressed. Auto-closes once nothing's left to show — e.g. every
        backgrounded process/watch it was showing has since exited/stopped."""
        watches = watches or []
        if not procs and not watches:
            self._hide_bgproc_detail()
            return
        selected_value = None
        if self.bgproc_detail_picker.items:
            selected_value = self.bgproc_detail_picker.items[
                self.bgproc_detail_picker.selected_index
            ].value
        items = self._bgproc_picker_items(procs, watches)
        self.bgproc_detail_picker.set_items(items)
        if selected_value is not None:
            for i, it in enumerate(items):
                if it.value == selected_value:
                    self.bgproc_detail_picker.selected_index = i
                    break
        self.request_render()

    _SUBAGENT_CHECKPOINT_STATUSES = {"DONE", "FAILED", "PAUSED", "NEEDS_INPUT"}
    # Subset of the above that's truly final — PAUSED/NEEDS_INPUT mean the
    # headless process already exited but still expects a relaunch with an
    # answer, so an entry in one of those statuses is never prune-eligible.
    _SUBAGENT_TERMINAL_STATUSES = {"DONE", "FAILED"}

    def _poll_bgprocs_tick(self) -> None:
        """Runs every 3s regardless of busy/idle state — a `cmd &` a previous
        turn backgrounded (or a monitor_(action="watch") stream started
        mid-turn) keeps running whether or not boss's own turn is active.
        Both are just in-memory dict lookups (see
        bash_tool.list_background_processes, monitor_watch_.list_watches),
        so unlike _poll_subagents_tick there's no disk I/O to justify a
        stat-gated skip — every tick just re-renders straight from them.

        Excludes any pid that's also a tracked subagent's own process — a
        `microcc-headless <dir> &` backgrounded through bash_ is both a
        survivor bash_tool sees via _track_survivors AND self-registers with
        subagent_tracker_ (see add_tracked), so without
        this filter the exact same process shows up twice: once as a
        subagent glyph, once as a raw PID pill. The two widgets are meant to
        be mutually exclusive for that pid — #subagent-status wins since it
        carries the richer status/summary."""
        from micro_cc.tools.bash_tool import list_background_processes
        from micro_cc.tools.monitor_watch_ import list_watches
        from micro_cc.utils import subagent_tracker_

        tracked = subagent_tracker_.read_tracked(self._project_dir)["subagents"]
        subagent_pids = {
            entry["pid"] for entry in tracked.values() if entry.get("pid") is not None
        }
        procs = [
            p for p in list_background_processes() if p["pid"] not in subagent_pids
        ]
        watches = list_watches()
        if self._bgproc_viewing:
            self._refresh_bgproc_detail(procs, watches)

        if not procs and not watches:
            self.bgproc_status.update("")
            self.request_render()
            return

        # Trimmed to just glyph + id/PID + age — the full command/
        # description used to be inlined here (truncated to 60 chars, still
        # often unreadable/wrapping across the status line for anything
        # non-trivial). Same info density as cc's own PID line; the actual
        # command is one ctrl+b away now (see _show_bgproc_detail) instead
        # of permanently taking up bottom-bar space. Watches get their own
        # glyph (◎ vs a plain process's ◇) so the two read as distinct kinds
        # of thing at a glance — same role monitor_.format_status_glyph's
        # per-status glyphs play for subagent_status.
        # A stalled process (see bash_tool._check_stall — output stopped growing and
        # looks like it's waiting on an interactive prompt) gets a distinct glyph/color
        # so it's visible at a glance without waiting for the model to next mention it.
        lines = [
            f"[{theme_store_.get('warn')}]⚠ {p['pid']} · {p['age']}s · stalled[/{theme_store_.get('warn')}]" if p.get("stalled")
            else f"◇ {p['pid']} · {p['age']}s"
            for p in procs
        ]
        lines += [f"◎ {w['watch_id']} · {w['age']}s" for w in watches]
        if lines:
            lines[0] += "  [dim]· ctrl+b to view[/dim]"
        self.bgproc_status.update("\n".join(self._cap_lines(lines)))
        self.request_render()

    def _poll_memory_review_tick(self) -> None:
        """Runs every 3s regardless of busy/idle state — same rationale as
        _poll_bgprocs_tick's own comment: msg_store_._review_memory is a
        fully decoupled background task (see its own docstring for why it's
        NOT awaited inline by compact_checkpoint), so it routinely finishes
        well after the turn that triggered it has already ended and the app
        is sitting idle. Polling only "once per model-response round" (the
        way _update_status_bar's own compaction check piggybacks on
        _refresh_status) would miss it in exactly that common case — this
        needs its own always-on timer, same as subagents/bgprocs.

        Cheap: one small JSON file read, only when it exists at all (most
        projects never trigger a memory-changing review)."""
        recap = load_memory_review_recap(self._project_dir)
        if recap is None or recap["ts"] == self._last_memory_review_ts:
            return
        self._last_memory_review_ts = recap["ts"]
        # Own widget/timer (_flash_memory_review), not _flash_status/
        # statusbar — see that method's docstring for why sharing the
        # compaction flash's slot would let the two stomp each other. Full
        # recap text, untruncated here — memory_flash itself (max_lines=5,
        # see _build_widget_tree) is what bounds it now, wrapping and
        # ellipsizing past 5 lines rather than this cutting it to one.
        # recap['recap'] is the model's own free-text summary of what it
        # changed in memory — untrusted the same way a subagent's name or a
        # backgrounded command is (see monitor_.format_status_glyph
        # and _poll_bgprocs_tick): this whole string is Rich markup parsed
        # by RichStatic.render() on the next tick, so an unescaped "[" (e.g.
        # the model mentioning a bracketed path like "[/some/file]" in its
        # own summary) raises MarkupError inside the render loop.
        self._flash_memory_review(f"[italic]✎ memory reviewed — {_markup_escape(recap['recap'])}[/italic]")

    def enter_subagent_view(self, target: str) -> None:
        """Swap the message area to show `target`'s (a subagent's
        project_dir) own conversation, read straight off disk — same
        load_msgs()/history_mount_() pair _reload_messages_from_disk uses
        for the boss's own conversation, just pointed at a different
        project_dir. No separate helper needed beyond this + the mirror
        exit_subagent_view below (see the module-level function
        load_msgs, imported at the top of this file — not a method)."""
        self._subagent_viewing_target = target
        self.subagent_message_list.clear()
        target_msgs = load_msgs(target)
        history_msgs = history_mount_(target_msgs)
        for m in history_msgs:
            self.subagent_message_list.add(MessageRow(m))
        self.root.replace(self.messages_scroll, self.subagent_scroll_view)
        self.subagent_scroll_view.scroll_to_end()
        self.request_render()

    def exit_subagent_view(self) -> None:
        """Back to the boss's own conversation. Reloads fresh from disk
        rather than trusting whatever was last in self.message_list —
        it could be stale if e.g. an incoming message_session_ delivery
        or a subagent wakeup landed while you were viewing someone
        else's transcript (see _inject_incoming_turn).

        Skipped while boss's own turn is actively running (shift+tab works
        mid-turn by design — see the binding's comment above). `container`
        in do_query/_stream_delta is self.message_list, captured once at
        turn start regardless of which widget root currently shows, so
        self.message_list has been kept live and in order the whole time
        subagent view was up — it's already more current than disk, since
        the in-flight streaming row and any delta not yet past a
        store_msgs checkpoint (tool_call/tool_result/done/...) never made
        it to messages.jsonl. _reload_messages_from_disk clears first and
        only finalizes/appends the streaming row after, so racing it here
        shoved that live text before the (stale) reloaded history — with
        follow="end" auto-scroll landing past it, this is what "streamed
        boss text vanishes on shift+tab back" actually was."""
        self._subagent_viewing_target = None
        self.root.replace(self.subagent_scroll_view, self.messages_scroll)
        self.messages_scroll.scroll_to_end()
        self.request_render()
        turn_active = self._current_query_task is not None and not self._current_query_task.done()
        if not turn_active:
            self._safe_task(self._reload_messages_from_disk(), "reload after subagent view exit")

    async def _poll_subagents_tick(self, *, cold_start: bool = False) -> None:
        """Runs every 3s regardless of busy/idle state (started in on_mount,
        stopped in on_unmount) — subagents work whether or not boss's own
        turn is active. Re-reads the tracker file fresh every tick (cheap:
        it's a small JSON file) so entries bash_'s auto-detect or a fresh
        monitor_(action="check") call added mid-session show up without
        needing a restart. See monitor_.poll_status for why this uses a
        stat-gated tail-read instead of a plain load_msgs per tracked dir.

        cold_start=True only on the one call on_mount fires directly (not
        the recurring timer) — i.e. this may be the very first tick this
        boss process has ever run, so any wakeup it fires here could be
        reporting a checkpoint that happened while boss was completely
        offline (crashed, closed, restarted). do_query always reloads
        load_msgs() fresh, so persisted history isn't lost — but a long
        history can get compressed into a checkpoint summary, and a resurrected
        process has had zero chance yet to re-orient itself the way a
        continuously-running boss would have across its own earlier /graph
        turn. _wake_for_subagent uses this to make the very first wakeup
        after a (re)start explicitly say so, instead of the routine
        "if you need it again" phrasing that's fine once boss already has
        live context.

        Display and wakeup are two independent predicates over each entry's
        own (status, notified) fields, not derived from list membership —
        an entry shows up here as long as it's non-terminal, or terminal but
        not yet notified. Wakeup fires (at most once per checkpoint) the
        instant status lands on DONE/FAILED/PAUSED/NEEDS_INPUT while
        unnotified, then mark_notified flips the flag under the same lock so
        a later tick can't fire it again. PAUSED/NEEDS_INPUT are non-terminal
        (see _SUBAGENT_TERMINAL_STATUSES) so they still wake boss once, but
        keep displaying/prune-ineligible afterward — the process already
        exited but still expects a relaunch, and a relaunch's add_tracked
        call is what resets notified for the next checkpoint."""
        from micro_cc.utils import subagent_tracker_
        from micro_cc.tools.monitor_ import poll_status, format_status_glyph
        from micro_cc.utils.inbox_store_ import peek_pending_count

        data = subagent_tracker_.read_tracked(self._project_dir)
        subagents = data["subagents"]
        self._tracked_subagents = list(subagents)

        if not subagents:
            self.subagent_status.update("")
            self.request_render()
            return

        display_lines = []
        wakeups = []
        for target, entry in subagents.items():
            cached = self._subagent_poll_cache.get(target)
            info, new_stat = poll_status(target, cached)
            self._subagent_poll_cache[target] = new_stat

            if info is None:
                # Unchanged since last poll — render whatever we last knew
                # rather than re-deriving it (that's the whole point of the
                # stat-gated skip: no file read at all on this branch).
                status = entry["status"]
                if status not in self._SUBAGENT_CHECKPOINT_STATUSES:
                    # Still "RUNNING" and the transcript hasn't moved — but
                    # that's exactly what a killed/crashed subagent looks
                    # like too, since nothing ever writes a terminal STATUS
                    # line for a process that's dead. Without this check an
                    # entry a user had boss `kill <pid>` stays displayed
                    # (and un-prunable) forever. Only fires once pid is on
                    # the entry (see add_tracked) — older
                    # entries from before that field existed just fall
                    # through unchanged, same as today.
                    pid = entry.get("pid")
                    if pid is not None:
                        try:
                            os.kill(pid, 0)
                        except ProcessLookupError:
                            name = os.path.basename(target.rstrip("/")) or target
                            info = {
                                "resolved": target,
                                "name": name,
                                "status": "FAILED",
                                "summary": "process is no longer running (stopped or killed before finishing)",
                                "pending": peek_pending_count(target),
                            }
                            status = info["status"]
                            subagent_tracker_.update_status(
                                self._project_dir, target, status
                            )
                            if not entry["notified"]:
                                wakeups.append((target, info))
                        except PermissionError:
                            pass  # pid got reused by something we don't own — can't tell, leave as-is
            else:
                status = info["status"]
                if status != entry["status"]:
                    subagent_tracker_.update_status(self._project_dir, target, status)
                if (
                    status in self._SUBAGENT_CHECKPOINT_STATUSES
                    and not entry["notified"]
                ):
                    wakeups.append((target, info))

            if status in self._SUBAGENT_TERMINAL_STATUSES and entry["notified"]:
                # Already told boss about this one — stop showing it on the
                # ambient line, but leave the entry for prune() to reap.
                continue

            name = (
                info["name"]
                if info is not None
                else (os.path.basename(target.rstrip("/")) or target)
            )
            pending = info["pending"] if info is not None else 0
            # Same "only fresh on an actual change" tradeoff as pending
            # above — tokens.json is written by the same model-call turn
            # that appends to messages.jsonl, so an unchanged messages.jsonl
            # stat means an unchanged token count too; no separate read
            # needed on the skip branch.
            # .get, not [] — the synthetic "process died" info dict built
            # above (pid liveness check) has no "tokens" key of its own.
            tokens = info.get("tokens") if info is not None else None
            # Two independent signals on the same line, not one merged
            # into the other: format_status_glyph's own glyph is the RUN
            # STATUS (spinner/checkmark/etc, unrelated to viewing); this
            # leading ●/○ is ONLY "is this the one currently swapped into
            # the message area" — target is still in scope here, which is
            # exactly why this has to happen in THIS loop and not the
            # later one that only had plain strings to work with.
            viewing_glyph = "●" if target == self._subagent_viewing_target else "○"
            display_lines.append(f"{viewing_glyph} {format_status_glyph(name, status, pending, tokens)}")

        # format_status_glyph (monitor_.py) stays a pure
        # (name, status, pending) -> text formatter — it has no business
        # knowing about self._subagent_viewing_target, that's UI state
        # only MicroTui holds. The viewing decoration is composed here,
        # at the call site, not pushed down into the tool module.
        if display_lines:
            boss_glyph = "●" if self._subagent_viewing_target is None else "○"
            # Only mention of shift+tab anywhere in the UI — the binding
            # itself (see the shift+tab branch in the terminal-input
            # handler above) has existed with no on-screen hint at all, so
            # the only way to discover it was already knowing it existed.
            # Attached to the boss line specifically since that's the one
            # line guaranteed to render whenever this whole block does.
            all_lines = [f"⚙ {boss_glyph} boss  [dim]· shift+tab to view[/dim]"] + [f"⚙ {line}" for line in display_lines]
            self.subagent_status.update("\n".join(self._cap_lines(all_lines)))
        else:
            self.subagent_status.update("")
        self.request_render()

        for target, info in wakeups:
            await self._wake_for_subagent(info, cold_start=cold_start)
            subagent_tracker_.mark_notified(self._project_dir, target)

        # Previously only reaped once, in start() at app launch — mid-session
        # a DONE/FAILED+notified entry (already hidden from the status line
        # above, but still sitting in tracked_subagents.json) lingered there
        # until the next restart. Safe to call every tick regardless of
        # whether this one produced any wakeups (see prune's own docstring:
        # an entry only becomes eligible once both the "worth showing" and
        # "boss was told" questions are already resolved, so this can never
        # race mark_notified above) — cheap when there's nothing to reap
        # (one read, no write; see prune's `changed` guard).
        subagent_tracker_.prune(self._project_dir)

    # Deliberately status-agnostic (no PAUSED/FAILED/DONE-specific
    # instructions here) — /graph's hidden prompt already covers what each
    # status means and what to do about it, and re-explaining that at every
    # wakeup would just duplicate it. What a wakeup actually can't assume is
    # that the model still HAS that context: /graph's explanation is a
    # one-time turn that can scroll out of a long conversation or get
    # folded into a checkpoint summary. So this only re-orients — reminds
    # the model it's mid-orchestration and where the real context lives —
    # rather than re-teaching the mechanism inline. The full mechanism
    # itself now lives in the graph-orchestration skill (durable, re-loadable
    # any time via read_skill, unlike the one-time /graph turn).
    _SUBAGENT_WAKEUP_REMINDER = (
        "You have subagents running — spawned via bash_ + microcc-headless. "
        "Call monitor_(action='check', targets=[project_dirs...]) for a full "
        "status check across all of them, and check GRAPH_PLAN.md in this project "
        "directory for what this means and what to do next. If you need the "
        "full orchestration mechanism again (tracking/wakeup/coordination "
        "details), call read_skill('graph-orchestration')."
    )

    # Only prefixed on the cold_start wakeup path (see _poll_subagents_tick's
    # docstring) — this checkpoint may be the first thing this boss process
    # has seen since a crash/restart, so unlike the routine reminder above
    # ("if you need it again"), this makes reloading the skill non-optional
    # before acting on the checkpoint.
    _SUBAGENT_COLD_START_PREFIX = (
        "[this is the first check since boss (re)started — you may have no "
        "live memory of this orchestration even if it's mid-flight] Call "
        "read_skill('graph-orchestration') now, before deciding anything, "
        "to reload the full mechanism — then read GRAPH_PLAN.md for the "
        "actual plan. Do not act on the checkpoint below until you have.\n\n"
    )

    async def _wake_for_subagent(self, info: dict, *, cold_start: bool = False) -> None:
        """A tracked subagent just hit a checkpoint boss hasn't seen yet —
        wake boss up the same way an incoming message_session_ call would,
        instead of leaving it to notice only if/when it happens to call
        monitor_ itself again."""
        from micro_cc.tools.monitor_ import format_status_line

        prefix = self._SUBAGENT_COLD_START_PREFIX if cold_start else ""
        content = (
            f"{prefix}[subagent checkpoint]\n{format_status_line(info)}\n\n"
            f"{self._SUBAGENT_WAKEUP_REMINDER}"
        )
        await self._inject_incoming_turn(content)

    # ============================================= subagents

    # ========================================================================== start | stop

    async def start(self):
        # Start background services
        state_store.start_cleanup_task()
        self._project_files = list_project_files(self._project_dir)
        self._session_server = await start_listener(
            self._project_dir, self._on_session_message
        )

        # monitor_(action="watch") streams deliver events by calling this
        # back the moment a matching line arrives — registered here (not at
        # import time) because a watch is only ever meaningful while an
        # interactive loop is actually standing by to receive the push; see
        # monitor_watch_'s own module docstring. start_watch refuses outright
        # if this was never set (headless/batch never calls start()).
        from micro_cc.tools import monitor_watch_ as _monitor_watch_

        _monitor_watch_.set_wake_callback(self._on_monitor_event)

        # Scheduled prompts (tools/monitor_schedule_) push the same way a
        # watch line does — a due task becomes its own turn via this same
        # handler. set_start_tick lets monitor_(action="schedule") nudge us to
        # (re)start the tick if a schedule is added after startup; both are
        # cleared implicitly by never being set on the headless path.
        from micro_cc.tools import monitor_schedule_runtime as _schedule_rt

        # A /theme switch repaints an already-running UI through this
        # listener — see _apply_theme (re-emits the terminal's default
        # bg/fg, then invalidates every cached render).
        theme_store_.on_change(self._apply_theme)
        _schedule_rt.set_wake_callback(self._on_monitor_event)
        _schedule_rt.set_start_tick(self._ensure_schedule_tick)
        self._ensure_schedule_tick()

        # Subagent orchestration — read whatever was tracked (by a previous
        # run's monitor_ calls, or bash_'s auto-detected spawns)
        # straight from disk, so a fresh boss process resumes exactly where
        # the last one left off instead of forgetting background subagents
        # ever existed. Poll immediately (don't wait for the first interval
        # tick) so a subagent that finished while this app was closed gets
        # surfaced right away. Prune once on startup — cheap, and it's the
        # one moment guaranteed not to race a wakeup that's still in flight.
        from micro_cc.utils import subagent_tracker_ as _subagent_tracker_

        _subagent_tracker_.prune(self._project_dir)
        self._subagent_poll_timer = asyncio.create_task(self._subagent_poll_loop())
        self._safe_task(self._poll_subagents_tick(cold_start=True), "subagent poll")

        # Background bash processes — cheap in-memory check (no subprocess,
        # no disk read), so a plain 3s interval is fine even while idle.
        self._bgproc_poll_timer = asyncio.create_task(self._bgproc_poll_loop())
        self._poll_bgprocs_tick()

        # Memory-review recap flash — see _poll_memory_review_tick for why
        # this needs its own always-on timer rather than piggybacking on
        # the status bar's per-round refresh.
        self._memory_review_poll_timer = asyncio.create_task(self._memory_review_poll_loop())

        # Restart-on-self-change — baseline the harness's own source and poll
        # it. Runs on every install (checkout, pip, pipx, brew, WSL2): the
        # harness edits its own package dir wherever it physically lives.
        self._init_self_manifest()
        self._self_poll_timer = asyncio.create_task(self._self_poll_loop())

        # Restore an unsent prompt stashed by a self-reload just before it
        # re-exec'd (utils/self_reload_.save_pending_prompt). Read-and-delete,
        # so it lands exactly once — in this, the process that came up after
        # the restart.
        from micro_cc.utils import self_reload_ as _self_reload

        _stashed = _self_reload.take_pending_prompt(self._project_dir)
        if _stashed:
            self.prompt.insert(_stashed)

        # Status bar
        self._refresh_status()
        self._static_hint_text()

        # Load history — mount each as a MessageRow component
        history_msgs = history_mount_(self.__existing_msgs)
        for m in history_msgs:
            self.message_list.add(MessageRow(m))
        self._msg_rows = history_msgs
        self.messages_scroll.scroll_to_end()

        # Force focus onto PromptInput so Enter is routed to
        # _on_prompt_submitted from the first frame.
        self.set_focus(self.prompt)

        # Screen is up — kick the update check off in the background so it
        # never delays first paint.
        self._safe_task(check_and_update(self, __version__), "update check")
        self._safe_task(_background_terminal_setup_check(self), "terminal setup check")

        # Phase 5: hand the real terminal over — raw mode, bracketed paste,
        # Kitty negotiation (ProcessTerminal.start()), alt-screen, and
        # register stdin with the event loop so keystrokes actually reach
        # the app. Guarded on isatty() the same way ProcessTerminal.start()
        # itself is — a piped/redirected stdin (tests, non-interactive
        # invocations) skips all of this and the app still renders and
        # responds to external stimuli (do_query turns via
        # message_session_/subagent wakeups), it just can't take keyboard
        # input, which is correct for that context.
        if sys.stdin.isatty():
            sys.stdout.write(ENTER_ALT_SCREEN)
            sys.stdout.flush()
            self.terminal.start(self._on_terminal_input, self._on_terminal_resize)
            asyncio.get_event_loop().add_reader(sys.stdin.fileno(), self._on_stdin_readable)
            self._stdin_reader_registered = True

        self._running = True
        self._render_task = asyncio.create_task(self._render_loop())
        self.request_render()

        # The app is genuinely up (widget tree built, history mounted, terminal
        # handed over) — tell the supervisor so a boot that reaches here can't
        # be mistaken for a failure-to-boot, and so the boot-failure counter
        # that a self-reload just incremented is cleared. See self_heal_.
        try:
            from micro_cc import self_heal_

            self_heal_.note_boot_ok(self._project_dir)
        except Exception:
            pass

    async def _subagent_poll_loop(self) -> None:
        while True:
            await asyncio.sleep(3.0)
            await self._poll_subagents_tick()

    async def _bgproc_poll_loop(self) -> None:
        while True:
            await asyncio.sleep(3.0)
            self._poll_bgprocs_tick()

    async def _memory_review_poll_loop(self) -> None:
        while True:
            await asyncio.sleep(3.0)
            self._poll_memory_review_tick()

    # --- scheduled prompts (tools/monitor_schedule_) -------------------------
    def _ensure_schedule_tick(self) -> None:
        """Start the schedule tick if it isn't already running. Called once at
        start() and again by monitor_(action="schedule") so a schedule created
        mid-session is picked up immediately rather than at the next restart.
        Idempotent — a second call while the task is live does nothing."""
        if self._schedule_poll_timer is not None and not self._schedule_poll_timer.done():
            return
        self._schedule_poll_timer = asyncio.create_task(self._schedule_poll_loop())

    async def _schedule_poll_loop(self) -> None:
        # 5s cadence: schedules have a 1-minute floor (cron granularity), so
        # this is already far tighter than the shortest possible gap between
        # fires — no need to tick faster.
        while True:
            await asyncio.sleep(5.0)
            self._poll_schedule_tick()

    def _poll_schedule_tick(self) -> None:
        """Fire every due schedule. The push goes through
        monitor_schedule_runtime's wake callback (set to _on_monitor_event in
        start()), so a due task arrives as its own turn exactly like a watch
        line or a subagent checkpoint. Wrapped so a bad schedule spec can never
        kill the tick task — same safety net _safe_task gives every other task."""
        from micro_cc.tools import monitor_schedule_runtime as _schedule_rt

        try:
            _schedule_rt.tick(self._project_dir)
        except Exception as e:
            self._show_error_row(f"schedule tick crashed: {type(e).__name__}: {e}")

    # ========================================================================== start

    # ==========================================================================

    async def stop(self):
        # _set_busy(False) already clears the OSC 9;4 progress indicator at
        # the end of every turn; exiting while idle needs no extra write,
        # and exiting mid-turn relies on the terminal clearing per-tab
        # state on process exit, same as every other OSC 9;4-aware
        # terminal already does.
        self._running = False
        # Unlike bash_'s own `cmd &` survivors (deliberately left running —
        # the whole point of backgrounding), a monitor watch is OUR subprocess
        # and nothing outside this process is ever going to read its events
        # again once we exit — kill_all so a `tail -f`/`while true` watch
        # doesn't leak past this session.
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
    """Entry point. start()/stop() own the full terminal lifecycle (raw
    mode, alt-screen, Kitty negotiation, stdin registered with the event
    loop — see MicroTui.start()) when stdin is a real tty; the run loop
    below just keeps the process alive until something sets
    app._running = False (/exit, /quit — see cmd_exit in
    screen_cmds_tui_.py) or Ctrl+C."""
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
        # `print` is neutered above (module-level, before this ever runs)
        # so a startup crash writes nothing anywhere by default — use the
        # saved real print + a log file so it's actually visible.
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
