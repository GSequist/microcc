from typing import TypedDict, List, NotRequired


class AskOption(TypedDict):
    label: str
    description: str
    preview: NotRequired[str]


class AskQuestion(TypedDict):
    question: str
    header: str
    options: List[AskOption]  # [] for a freeform/chat question
    multiSelect: bool


_REQUIRED_QUESTION_KEYS = ("question", "header", "options", "multiSelect")


def validate_ask_questions(questions) -> str | None:
    """Check `questions` against the AskQuestion shape before it ever reaches
    the TUI/webui, which index straight into each dict (q["question"], etc.)
    with no fallback. Returns an error string describing what's wrong (for
    an is_error tool_result so the model can retry), or None if it's fine."""
    if not isinstance(questions, list) or not questions:
        return "questions must be a non-empty list"
    for i, q in enumerate(questions):
        if not isinstance(q, dict):
            return f"questions[{i}] must be an object"
        missing = [k for k in _REQUIRED_QUESTION_KEYS if k not in q]
        if missing:
            return f"questions[{i}] missing required field(s): {', '.join(missing)}"
        if not isinstance(q["options"], list):
            return f"questions[{i}].options must be a list"
        for j, opt in enumerate(q["options"]):
            if not isinstance(opt, dict) or "label" not in opt or "description" not in opt:
                return f"questions[{i}].options[{j}] must be an object with 'label' and 'description'"
    return None


async def ask_user_question_tool_(
    questions: List[AskQuestion],
    *,
    project_dir,
    model,
) -> str:
    """
    Ask the user one or more questions, shown one at a time in order. Each
    question independently picks one of three modes:
      - freeform: options=[] — user types a real text answer
      - single-choice: options set, multiSelect=false — user picks exactly one
      - multi-choice: options set, multiSelect=true — user toggles any number
        of options, then confirms

    Args:
        questions: Each question shown to the user in order. Leave `options`
            empty for a freeform question the user answers by typing.
            Set `multiSelect` true to let the user pick more than one option.
            Set an option's `preview` (markdown) for a mockup, code snippet,
            or other comparison content shown alongside the option list as
            the user moves their cursor over it — use it for content that's
            genuinely easier to compare visually, not to restate the
            description in a bigger box.
    """
    return questions
