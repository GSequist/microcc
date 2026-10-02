"""Mounts the REAL MicroTui (start_live_tui_.py) — not standalone
component instances — and renders it through its own real TuiAltScreen,
with dummy claude_loop-shaped messages standing in for a real turn. This
is what production do_render() actually produces, overlays and all, so
what you see here is what "start_live_tui_ working" looks like today.

PromptInput is the one piece that isn't finished (prompt_input_.py still
can't import — TextArea is undefined). Rather than touch that file, this
script injects a temporary stub into sys.modules before importing
MicroTui, ONLY if the real import fails, so it never overwrites or
interferes with what you're building there — swap it out for real the
moment prompt_input_.py imports cleanly, nothing else here changes.

Usage:
    source env/bin/activate
    python tests/manual/tui_native_demo_.py [project_dir]
Press Enter to exit (restores the terminal out of alt-screen).
"""

import os
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

try:
    import micro_cc.tui_native.prompt_input_  # noqa: F401 — real import, if it works, nothing to stub
except Exception as e:
    print(f"(prompt_input_.py not importable yet: {e!r} — using a temporary stub for this demo only)", file=sys.stderr)

    class _StubPromptInput:
        """Placeholder ONLY for this demo — not written back to
        prompt_input_.py. Satisfies just enough of the shape MicroTui's
        widget tree expects (render/invalidate + .placeholder/.text) so
        the real MicroTui can be constructed and rendered."""

        def __init__(self):
            self.text = ""
            self.placeholder = ""

        def render(self, width: int) -> list[str]:
            shown = self.placeholder or self.text or ""
            return [f" {shown}"[:width]]

        def invalidate(self) -> None:
            pass

        def focus(self) -> None:
            pass

        def clear(self) -> None:
            self.text = ""

    stub_module = types.ModuleType("micro_cc.tui_native.prompt_input_")
    stub_module.PromptInput = _StubPromptInput
    sys.modules["micro_cc.tui_native.prompt_input_"] = stub_module

from micro_cc.tui_native.alt_screen_ import ENTER_ALT_SCREEN, EXIT_ALT_SCREEN
from micro_cc.tui_native.message_row_ import MessageRow
from micro_cc.tui_native.list_picker_ import PickerItem
from micro_cc.start_live_tui_ import MicroTui


SAMPLE_MESSAGES = [
    {"type": "user", "content": "explain how the scroll view follows the bottom"},
    {"type": "text", "content": "# heading\n\nSome **bold** and `inline code`, then a fence:\n\n```python\ndef f(x):\n    return x + 1\n```"},
    {"type": "thinking", "content": "weighing whether ScrollView or VStack owns this..."},
    {"type": "tool_call", "name": "bash_", "input": {"command": "git status"}, "result": "\x1b[32mM src/foo.py\x1b[0m\n\x1b[31mD src/bar.py\x1b[0m", "expanded": True},
    {"type": "tool_call", "name": "bash_", "input": {"command": "ls -la"}, "result": "total 12\ndrwxr-xr-x  a  b  c", "expanded": False},
    {"type": "error", "content": "No API endpoint configured — run /login"},
    {"type": "approval", "name": "bash_", "input": {"command": "rm -rf build/"}, "expanded": False},
    {"type": "user_queued", "content": "second question, queued mid-turn"},
]


def main() -> None:
    project_dir = sys.argv[1] if len(sys.argv) > 1 else os.getcwd()

    app = MicroTui(project_dir, messages=[])

    # Mirrors what start()/_mount_row do internally, minus load_msgs/
    # history_mount_ — these ARE the target shape etype_handler_ mounts,
    # just invented directly instead of coming from a real claude_loop turn.
    for msg in SAMPLE_MESSAGES:
        app.message_list.add(MessageRow(msg))
    app._msg_rows = list(SAMPLE_MESSAGES)
    app.messages_scroll.scroll_to_end()

    # Real status/hint methods, not fakes — _static_hint_text() is the
    # actual production method.
    app._static_hint_text()
    app.statusbar.update("⏣ demo-project | ◈ claude-sonnet-5")

    # What _poll_memory_review_tick's _flash_memory_review call renders once
    # msg_store_._review_memory finishes making a real change — see that
    # function's change_log ("+ key" / "~ key" / "- key" per add/edit/
    # delete). This is its OWN widget (self.memory_flash), separate from
    # statusbar — see _flash_memory_review's docstring for why: a review
    # runs right after a compaction fold, so the two flashes can genuinely
    # be in flight together, and sharing one slot would let whichever fires
    # second stomp the other. Lives ABOVE the prompt (with working/
    # ask_question_text — see bottom_bar.add order in _build_widget_tree),
    # not down by statusbar. max_lines=5: full untruncated text, wrapping
    # naturally like ask_question_text does, capped so a long change_log
    # can't take over the screen — narrow this terminal to see the "…"
    # overflow marker kick in past 5 wrapped lines. Set directly rather
    # than via _flash_memory_review() itself: that method arms a restore
    # timer through _call_later -> asyncio.create_task, which needs a
    # running event loop this synchronous demo doesn't have. This is
    # exactly the string _flash_memory_review would have written to the
    # widget first, before arming that timer — the part that's actually on
    # screen. Shown alongside the normal statusbar line below, not instead
    # of it — both are independently visible, exactly the realistic case
    # right after a compaction.
    app.memory_flash.update(
        "[italic]✎ memory reviewed — "
        "+ User wants git commits without AI co-author trailer, ever; "
        "~ Deep memory removed, replaced by review-on-compaction; "
        "+ search_history_ tool added for forever lookback over folded messages; "
        "- stale entry about the old DEEP_MEMORY_PROMPT flow[/italic]"
    )
    app.working.update("⠋ Working…")
    app.subagent_status.update("⚙ headless-worker-1: RUNNING (3 pending)")
    app.bgproc_status.update("◇ PID 4821: npm run dev  (312s)")

    # Real overlay — exercises the same TuiAltScreen.show_overlay path a
    # live /model command would.
    app.model_picker.set_items([
        PickerItem("opus", "claude-opus-5", "most capable, slowest"),
        PickerItem("sonnet", "claude-sonnet-5", "balanced (default)"),
        PickerItem("haiku", "claude-haiku-4-5", "fastest, cheapest"),
    ])
    app.model_picker.selected_index = 1
    app._open_picker(app.model_picker, "_model_picker_overlay")

    sys.stdout.write(ENTER_ALT_SCREEN)
    sys.stdout.flush()
    try:
        app.tui.do_render()
        input()  # blocks here; Enter exits — terminal isn't in raw mode, this is safe
    finally:
        sys.stdout.write(EXIT_ALT_SCREEN)
        sys.stdout.flush()


if __name__ == "__main__":
    main()
