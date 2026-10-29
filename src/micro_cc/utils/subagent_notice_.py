"""<subagent-notification> wake payload: built by monitor_.checkpoint_turn, parsed by the TUI and webui renderers."""

import re

TAG = "subagent-notification"
RESULT_CAP = 8000
_FIELDS = ("name", "project_dir", "status", "summary", "result", "usage", "unread_mail", "transcript")
_BLOCK_RE = re.compile(rf"<{TAG}>(.*?)</{TAG}>", re.S)
_LEGACY_HEAD = "[subagent checkpoint]"
_LEGACY_LINE = re.compile(r"^\S+ (.+?) — ([A-Z_]+): (.*?)(?:  \[\d+ unread mail\])?(?:  \[[\d,]+ tokens\])?$")

# Cold-start prefix: after a restart the model may have no memory, so reloading the skill is mandatory.
COLD_START_PREFIX = (
    "[this is the first check since boss (re)started — you may have no "
    "live memory of this orchestration even if it's mid-flight] Call "
    "read_skill('graph-orchestration') now, before deciding anything, "
    "to reload the full mechanism — then read GRAPH_PLAN.md for the "
    "actual plan. Do not act on the notification below until you have.\n\n"
)
_COLD_RE = re.compile(r"^\[this is the first check since boss \(re\)started.*?\n\n", re.S)


def cap_result(text: str, transcript: str) -> str:
    """Head of a long reply plus a pointer to where the rest is."""
    if len(text) <= RESULT_CAP:
        return text
    return f"{text[:RESULT_CAP]}\n… [truncated {len(text) - RESULT_CAP} chars — full reply in {transcript}]"


def _esc(v) -> str:
    return str(v).replace("&", "&amp;").replace("<", "&lt;")


def _unesc(v: str) -> str:
    return v.replace("&lt;", "<").replace("&amp;", "&")


def block(**fields) -> str:
    """One notification block; values escaped so a reply can't close a tag, empty fields left out."""
    lines = [f"<{k}>{_esc(v)}</{k}>" if k != "result" else f"<result>\n{_esc(v)}\n</result>"
             for k in _FIELDS if (v := fields.get(k)) not in (None, "", 0)]
    return f"<{TAG}>\n" + "\n".join(lines) + f"\n</{TAG}>"


def _tag(body: str, key: str) -> str:
    m = re.search(rf"<{key}>(.*?)</{key}>", body, re.S)
    return _unesc(m.group(1).strip()) if m else ""


def parse(text: str) -> dict | None:
    """{notes, text} for a wake turn (blocks first, cold-start prefix allowed); text is whatever follows them.
    None if the turn doesn't start with a notification, so a pasted block stays a normal message."""
    body = _COLD_RE.sub("", text.strip(), count=1)
    if body.startswith(f"<{TAG}>"):
        notes = [{k: _tag(b, k) for k in _FIELDS} for b in _BLOCK_RE.findall(body)]
        return {"notes": notes, "text": _BLOCK_RE.sub("", body).strip()}
    if not body.startswith(_LEGACY_HEAD):
        return None
    # Pre-notification "[subagent checkpoint]" turns: one status line per child, then the old reminder (dropped).
    notes = []
    for line in body[len(_LEGACY_HEAD):].strip().splitlines():
        m = _LEGACY_LINE.match(line.strip())
        if not m:
            break
        notes.append({k: "" for k in _FIELDS} | {"name": m[1], "status": m[2], "summary": m[3]})
    return {"notes": notes, "text": ""} if notes else None
