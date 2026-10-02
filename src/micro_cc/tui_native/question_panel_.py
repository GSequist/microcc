"""Stage-walking composite for ask_user_question_tool_'s AskQuestion list —
the native replacement for _show_current_ask_question/_advance_ask_user/
_hide_ask_ui in start_live_.py. Mirrors that flow exactly: one question
shown at a time, in order, three mutually exclusive modes per stage
(multiSelect -> MultiSelectList, single-select -> ListPicker, options=[]
-> freeform text routed to the real prompt input). No Future inside this
component — its eventual call site keeps using the existing
_pending_input/_pending_result Event pair unchanged; this only exposes
plain on_done(answers)/on_cancel() callback attributes, same shape as
ListPicker/MultiSelectList.

Expects each question as the AskQuestion TypedDict shape from
tools/ask_user_tool.py: {question, header, options: [{label, description}], multiSelect}.
"""

from micro_cc.tui_native.list_picker_ import ListPicker, PickerItem
from micro_cc.tui_native.multi_select_list_ import MultiSelectList
from micro_cc.tui_native.theme_ import default_theme, Theme


def _options_to_items(options: list[dict]) -> list[PickerItem]:
    return [PickerItem(value=opt["label"], label=opt["label"], description=opt.get("description", ""))
            for opt in options]


class QuestionPanel:
    def __init__(self, questions: list[dict], theme: Theme | None = None):
        self.theme = theme or default_theme()
        self.questions = questions
        self.stage = 0
        self.answers: dict[str, object] = {}

        self.on_done = None    # Callable[[dict[str, object]], None]
        self.on_cancel = None  # Callable[[], None]

        self._component: ListPicker | MultiSelectList | None = None
        self._build_stage()

    # --- current stage introspection ------------------------------------
    @property
    def current(self) -> dict:
        return self.questions[self.stage]

    @property
    def current_question_text(self) -> str:
        return self.current["question"]

    @property
    def current_header(self) -> str:
        return self.current["header"]

    @property
    def is_text_stage(self) -> bool:
        return not self.current.get("options")

    def _build_stage(self) -> None:
        q = self.current
        if not q.get("options"):
            self._component = None
            return
        items = _options_to_items(q["options"])
        if q.get("multiSelect"):
            comp = MultiSelectList(items, theme=self.theme)
            comp.on_confirm = lambda checked: self._advance([it.label for it in checked])
            comp.on_cancel = self._cancel
        else:
            comp = ListPicker(items, theme=self.theme)
            comp.on_select = lambda it: self._advance(it.label)
            comp.on_cancel = self._cancel
        self._component = comp

    # --- freeform stage entry point (called by whatever owns the real
    # prompt input, since this panel doesn't own a text box itself) ------
    def submit_text_answer(self, text: str) -> None:
        if not self.is_text_stage:
            return
        self._advance(text)

    # --- stage advancement, mirrors _advance_ask_user exactly -----------
    def _advance(self, answer) -> None:
        self.answers[self.current_header] = answer
        self.stage += 1
        if self.stage >= len(self.questions):
            if self.on_done:
                self.on_done(self.answers)
            return
        self._build_stage()

    def _cancel(self) -> None:
        if self.on_cancel:
            self.on_cancel()

    # --- input delegation --------------------------------------------------
    def handle_key(self, key: str) -> bool:
        if self._component is None:
            return False
        return self._component.handle_key(key)

    def set_filter(self, text: str) -> None:
        if self._component is not None:
            self._component.set_filter(text)

    def invalidate(self) -> None:
        if self._component is not None:
            self._component.invalidate()

    # --- rendering -----------------------------------------------------
    def render(self, width: int) -> list[str]:
        if self._component is None:
            return []  # freeform stage: the real PromptInput does the rendering
        return self._component.render(width)
