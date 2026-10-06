"""Status marker convention: DONE/NEEDS_INPUT/FAILED in final text."""
import re

STATUS_INSTRUCTIONS = (
    "When you are done, end your reply with exactly one line and nothing after it:\n"
    "STATUS: DONE — <one-line summary of what you did>\n"
    "or STATUS: NEEDS_INPUT — <one-line question for the user>\n"
    "or STATUS: FAILED — <one-line reason you could not finish>"
)
_STATUS_RE = re.compile(r"STATUS:\s*(DONE|NEEDS_INPUT|FAILED|PAUSED)\s*[—-]\s*(.+)", re.IGNORECASE)
SYMBOL = {"DONE": "✓", "FAILED": "✗", "NEEDS_INPUT": "?", "UNKNOWN": "?", "PAUSED": "⏸", "RUNNING": "▶"}  # RUNNING tracked for completeness
def color(status: str) -> str:
    """Map status to color from active theme."""
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
    """Return (status, summary, body); UNKNOWN if no status marker."""
    match = _STATUS_RE.search(final_content)
    if not match:
        last_line = final_content.strip().splitlines()[-1][:100]
        return "UNKNOWN", last_line, final_content.strip()
    status = match.group(1).upper()
    summary = match.group(2).strip()
    body = final_content[: match.start()].strip()
    return status, summary, body
