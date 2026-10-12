"""Tool-approval prompt on the OptionPanel; module functions taking the app."""

from micro_cc.tui_native.option_panel_ import PanelItem

SUMMARY_CHARS = 160


def _summary(inp) -> str:
    text = " ".join(f"{k}: {v}" for k, v in inp.items()) if isinstance(inp, dict) else str(inp)
    text = " ".join(text.split())
    return text if len(text) <= SUMMARY_CHARS else text[:SUMMARY_CHARS - 1] + "\u2026"


def show(app, name: str, inp) -> None:
    app.statusbar.update("")
    app.static_hintbar.update("")
    app._option_panel.open(
        [PanelItem("yes", "Yes"), PanelItem("no", "No")],
        on_select=lambda it: resolve(app, it.value == "yes"),
        on_cancel=lambda: resolve(app, False),
        title=f"Allow {name}?  {_summary(inp)}",
    )


def resolve(app, approved: bool) -> None:
    if app._pending_input is not None and not app._pending_input.is_set():
        app._pending_result = approved
        app._pending_input.set()


def hide(app) -> None:
    app._option_panel.close()
    app._refresh_status()
    app._static_hint_text()
