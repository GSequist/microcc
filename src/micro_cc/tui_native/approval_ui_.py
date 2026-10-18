"""Tool-approval prompt on the OptionPanel; module functions taking the app."""

from micro_cc.tui_native.option_panel_ import PanelItem
from micro_cc.tui_native.theme_ import _sgr
from micro_cc.utils import theme_store_
from micro_cc.utils.tool_safety_ import explain_gated, gate_risk

SUMMARY_CHARS = 160

# risk -> (theme color key, label)
_RISK = {"low": ("ok", "Low risk"), "medium": ("warn", "Changes things"), "high": ("error", "High risk")}


def _summary(inp) -> str:
    """Raw args minus description, single line; the full call sits in the tool row above."""
    if isinstance(inp, dict):
        text = " ".join(f"{k}: {v}" for k, v in inp.items() if k != "description")
    else:
        text = str(inp)
    text = " ".join(text.split())
    return text if len(text) <= SUMMARY_CHARS else text[:SUMMARY_CHARS - 1] + "… (full call in the row above)"


def _badge(risk: str, reason: str) -> str:
    color, label = _RISK[risk]
    text = f"{label} · {reason}" if reason else label
    return f"{_sgr(theme_store_.get(color))}● {text}\x1b[39m"  # 39 = default fg, keeps bold


def show(app, name: str, inp) -> None:
    app.statusbar.update("")
    app.static_hintbar.update("")
    risk, reason = gate_risk(name, inp, app._project_dir)
    app._option_panel.open(
        [
            PanelItem("yes", "Yes"),
            PanelItem("always", "Yes, always", f"stop asking for {name}"),
            PanelItem("no", "No", "Model is told and asks what to do instead"),
        ],
        on_select=lambda it: resolve(app, {"yes": True, "always": "always", "no": False}[it.value]),
        on_cancel=lambda: resolve(app, False),
        title=f"{_badge(risk, reason)}  {explain_gated(name, inp)}\n{name} · {_summary(inp)}",
    )


def resolve(app, approved) -> None:
    """approved: True, False, or "always" (run and ungate the tool)."""
    if app._pending_input is not None and not app._pending_input.is_set():
        app._pending_result = approved
        app._pending_input.set()


def hide(app) -> None:
    app._option_panel.close()
    app._refresh_status()
    app._static_hint_text()
