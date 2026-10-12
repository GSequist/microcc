"""Ask-user question flow on the OptionPanel; module functions taking the app."""

from micro_cc.tui_native.option_panel_ import PanelItem
from micro_cc.tui_native.panel_layout_ import fit


def start(app, questions: list) -> None:
    app._ask_questions, app._ask_stage, app._ask_answers = questions, 0, {}
    show_current(app)


def show_current(app) -> None:
    q = app._ask_questions[app._ask_stage]
    app.statusbar.update("")
    app.static_hintbar.update("")
    app.ask_question_text.update("")
    if not q["options"]:
        # Freeform stage: question above the real prompt, which is the answer box.
        app._option_panel.close()
        app.ask_question_text.update(q["question"])
        app.prompt.placeholder = q["question"]
        return
    items = [PanelItem(o["label"], o["label"], o.get("description", "")) for o in q["options"]]
    app._option_panel.max_visible, app._option_panel.max_desc_lines = fit(app, items, q["question"])
    multi = bool(q.get("multiSelect"))
    app._option_panel.open(
        items,
        on_select=(lambda its: advance(app, [it.label for it in its])) if multi
        else (lambda it: advance(app, it.label)),
        on_cancel=lambda: cancel(app),
        title=q["question"],
        multi=multi,
    )


def advance(app, answer) -> None:
    q = app._ask_questions[app._ask_stage]
    app._ask_answers[q["header"]] = answer
    app._ask_stage += 1
    if app._ask_stage >= len(app._ask_questions):
        app._pending_result = app._ask_answers
        app._pending_input.set()
    else:
        show_current(app)


def cancel(app) -> None:
    if app._pending_input is not None and not app._pending_input.is_set():
        app._pending_result = None
        app._pending_input.set()


def hide(app) -> None:
    app._option_panel.close()
    app.ask_question_text.update("")
    app.prompt.placeholder = ""
    app._refresh_status()
    app._static_hint_text()
