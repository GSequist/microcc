"""Stable facade handed to mods as `api`; the raw app never leaves this module."""

from types import SimpleNamespace

from micro_cc import mods_
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
    )
