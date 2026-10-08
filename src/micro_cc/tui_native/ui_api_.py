"""Stable facade handed to mods as `api`; the raw app never leaves this module."""

from types import SimpleNamespace

from micro_cc import mods_
from micro_cc.tui_native import pane_
from micro_cc.utils.tokenization_simple import token_stats


def make_ui(app) -> SimpleNamespace:
    def notify(text: str) -> None:
        app._flash_status(str(text), seconds=4)

    def get_input() -> str:
        return app.prompt.text

    def set_input(text: str) -> None:
        app.prompt.clear()
        app.prompt.insert(str(text))
        app.request_render()

    def send_prompt(text: str) -> None:
        app._safe_task(app._on_prompt_submitted(str(text), literal=True), "mod send_prompt")

    def refresh() -> None:
        mods_.bump()
        app.request_render()

    def transcript_path(project_dir: str | None = None) -> str:
        """messages.jsonl of this project or a subagent's; absent under Postgres storage."""
        from micro_cc.utils.msg_store_ import _get_storage_dir
        return str(_get_storage_dir(project_dir or app._project_dir) / "messages.jsonl")

    return SimpleNamespace(
        notify=notify,
        get_input=get_input,
        set_input=set_input,
        send_prompt=send_prompt,
        refresh=refresh,
        usage=lambda: dict(token_stats),
        project_dir=app._project_dir,
        model=lambda: app._current_model,
        request_render=app.request_render,
        open_pane=lambda name, width="40%", side="right": pane_.open_pane(app, str(name), width, side=side),
        close_pane=lambda: pane_.close_pane(app),
        pane_open=lambda: app._pane_name,
        focus_pane=lambda on=True: pane_.focus(app, on),
        subagents=lambda: [dict(s) for s in app._subagent_snapshot],
        transcript_path=transcript_path,
    )
