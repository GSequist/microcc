"""Shared DONE/NEEDS_INPUT/FAILED status-marker convention used by
start_headless_.py's single-prompt run, so its outcome is machine-parseable
and terminal-printable without a dedicated "ask the user" tool: the model
just says so in its final text.
"""
import re

STATUS_INSTRUCTIONS = (
    "When you are done, end your reply with exactly one line and nothing after it:\n"
    "STATUS: DONE — <one-line summary of what you did>\n"
    "or STATUS: NEEDS_INPUT — <one-line question for the user>\n"
    "or STATUS: FAILED — <one-line reason you could not finish>"
)
_STATUS_RE = re.compile(r"STATUS:\s*(DONE|NEEDS_INPUT|FAILED|PAUSED)\s*[—-]\s*(.+)", re.IGNORECASE)
# RUNNING isn't a marker value a model ever writes (it means "no completed
# turn yet" — see monitor_.get_subagent_status) but it's tracked
# here alongside the real marker statuses so callers have one symbol/color
# table for every state a subagent can be in, instead of RUNNING silently
# reusing "?" and looking identical to NEEDS_INPUT in the ambient status line.
SYMBOL = {"DONE": "✓", "FAILED": "✗", "NEEDS_INPUT": "?", "UNKNOWN": "?", "PAUSED": "⏸", "RUNNING": "▶"}
def color(status: str) -> str:
    """Status -> color, from the active theme set. A function, not a dict,
    so a live theme switch is picked up on the next render instead of
    freezing whatever palette was active at import time."""
    from micro_cc.utils import theme_store_
    return {
        "DONE": theme_store_.get("ok"),
        "FAILED": theme_store_.get("error"),
        "NEEDS_INPUT": theme_store_.get("warn"),
        "UNKNOWN": theme_store_.get("warn"),
        "PAUSED": theme_store_.get("paused"),
        "RUNNING": theme_store_.get("running"),
    }.get(status, theme_store_.get("warn"))


def parse(final_content: str):
    """Split a model's final reply into (status, summary, body) — body is
    final_content with the trailing STATUS line stripped out, so callers
    can print the marker as a header/symbol instead of showing it twice.
    status is 'UNKNOWN' if the model didn't include the marker at all, in
    which case body is the untouched full reply and summary is just its
    last line, so the run still surfaces something instead of looking like
    a silent pass.
    """
    match = _STATUS_RE.search(final_content)
    if not match:
        last_line = final_content.strip().splitlines()[-1][:100]
        return "UNKNOWN", last_line, final_content.strip()
    status = match.group(1).upper()
    summary = match.group(2).strip()
    body = final_content[: match.start()].strip()
    return status, summary, body
