"""amend_last_reply_: revise the previous final reply in place; UIs fold the correction turn under it."""

from micro_cc.utils.system_prompt_ import HEAD_KEY, PATCH_KEY

AMENDED_KEY = "_amended"  # on the revised reply: how many times it was amended
AMEND_OK = "Amended in place"  # prefix of a successful amend's tool result; annotate() keys on it
ACK_KEY = "_amend_ack"  # on the reply closing a correction turn: UIs fold that turn


def text_of(content) -> str:
    if isinstance(content, str):
        return content
    return "".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")


def _hidden(msg: dict) -> bool:
    """A harness-injected user row the user never typed (system patch, reminder, nudge)."""
    content = msg.get("content")
    return (PATCH_KEY in msg or bool(msg.get("_ephemeral"))
            or (isinstance(content, str) and content.lstrip().startswith("<system-reminder>")))


def _is_final_reply(msg: dict) -> bool:
    content = msg.get("content")
    if isinstance(content, str):
        return bool(content.strip())
    if not isinstance(content, list):
        return False
    types = {b.get("type") for b in content if isinstance(b, dict)}
    return "text" in types and types <= {"text", "thinking"}


def find_target(msgs: list, turn_start: int) -> int | None:
    """Index of the final reply that ended the previous turn, or None if that turn ended any other way."""
    for i in range(min(turn_start, len(msgs)) - 1, -1, -1):
        m = msgs[i]
        if m.get("role") == "assistant":
            return i if _is_final_reply(m) else None
        if not _hidden(m):
            return None  # a real user message with no reply between it and this turn
    return None


def apply(msg: dict, old_str: str, new_str: str) -> tuple[dict | None, str]:
    """(amended copy, "") or (None, error). Only text changes; thinking and its signature are kept as-is."""
    if not old_str:
        return None, "Error: old_str is empty."
    content = msg.get("content")
    blocks = [{"type": "text", "text": content}] if isinstance(content, str) else [dict(b) for b in content]
    texts = [b for b in blocks if b.get("type") == "text"]
    hits = sum(b["text"].count(old_str) for b in texts)
    if hits == 0:
        return None, "Error: old_str not found in your previous reply. Copy it exactly."
    if hits > 1:
        return None, f"Error: old_str matches {hits} times in your previous reply. Include more surrounding text."
    for b in texts:
        if old_str in b["text"]:
            b["text"] = b["text"].replace(old_str, new_str, 1)
    if not "".join(b["text"] for b in texts).strip():
        return None, "Error: the amended reply would be empty."
    new_content = blocks[0]["text"] if isinstance(content, str) else blocks
    return {**msg, "content": new_content, AMENDED_KEY: msg.get(AMENDED_KEY, 0) + 1}, ""


def fold_spans(msgs: list) -> dict[int, int]:
    """{correction user idx: ack idx} for every folded correction turn."""
    spans = {}
    for i, m in enumerate(msgs):
        if m.get("role") != "assistant" or not m.get(ACK_KEY):
            continue
        for j in range(i - 1, -1, -1):
            u = msgs[j]
            if u.get("role") == "user" and isinstance(u.get("content"), str) and not _hidden(u):
                spans[j] = i
                break
    return spans


def pure_correction(msgs: list, turn_start: int) -> bool:
    """True if this turn (msgs[turn_start:]) only amended and read: safe for UIs to fold."""
    from micro_cc.tools.use_tool_ import resolve_call
    from micro_cc.utils.tool_safety_ import is_read_only

    typed = 0
    for m in msgs[turn_start:]:
        content = m.get("content")
        if m.get("role") == "user" and isinstance(content, str) and not _hidden(m):
            typed += 1
        if m.get("role") == "assistant" and isinstance(content, list):
            for b in content:
                if isinstance(b, dict) and b.get("type") == "tool_use":
                    name, args = resolve_call(b)
                    if name != "amend_last_reply_" and not is_read_only(name, args or {}):
                        return False
    return typed <= 1  # a message spliced in mid-turn means more happened than the correction


def make_tool(msgs: list, turn_start: int, project_dir: str, on_amend):
    """The per-turn amend_last_reply_ tool, bound to this turn's live msgs."""
    async def amend_last_reply_(old_str: str, new_str: str) -> str:
        """Fix a passage of your previous reply in place when the user points out an error in it, instead of restating the whole answer. Afterwards that reply shows only the corrected text, while the user's message still refers to the original. Keep your answer to this turn to one short line.

        Args:
            old_str: Exact text from your previous reply; must occur once.
            new_str: Replacement text.
        """
        from micro_cc.utils.msg_store_ import amend_msg, load_checkpoint

        idx = find_target(msgs, turn_start)
        if idx is None:
            return "Error: your previous turn did not end in a plain text reply, so there is nothing to amend. Answer normally."
        checkpoint = load_checkpoint(project_dir)
        if checkpoint and idx < checkpoint["as_of_index"]:
            return "Error: your previous reply is already compacted into the summary. Answer normally."
        amended, err = apply(msgs[idx], old_str, new_str)
        if err:
            return err
        after = [*msgs[:idx], amended, *msgs[idx + 1:]]
        if not amend_msg(project_dir, msgs[idx], amended, after):
            return "Error: your previous reply is no longer in stored history (rewound or compacted). Answer normally."
        msgs[idx] = amended
        on_amend()
        return (
            f"{AMEND_OK}: your previous reply (the assistant message before the user's last message) now reads "
            f"{new_str!r} where it said {old_str!r}. The user's message refers to the original wording, which no longer "
            "appears above; that is expected. The user sees only the corrected reply. Acknowledge the fix in one short line."
        )

    return amend_last_reply_


def _clip(text: str, n: int = 400) -> str:
    return text if len(text) <= n else text[:n] + "…"


def annotate(msgs: list) -> list:
    """Wire copy: each correction message gets a hidden note of the amends it caused, so the model reads them in order."""
    from micro_cc.tools.use_tool_ import resolve_call

    ok_ids = {b.get("tool_use_id") for m in msgs if m.get("role") == "user" and isinstance(m.get("content"), list)
              for b in m["content"] if isinstance(b, dict) and b.get("type") == "tool_result"
              and str(b.get("content", "")).startswith(AMEND_OK)}
    if not ok_ids:
        return msgs
    edits, last_user = {}, None
    for i, m in enumerate(msgs):
        content = m.get("content")
        if m.get("role") == "user" and not _hidden(m):
            blocks = content if isinstance(content, list) else []
            if isinstance(content, str) or not any(isinstance(b, dict) and b.get("type") == "tool_result" for b in blocks):
                last_user = i
        elif m.get("role") == "assistant" and isinstance(content, list) and last_user is not None:
            for b in content:
                if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("id") in ok_ids:
                    name, args = resolve_call(b)
                    if name == "amend_last_reply_":
                        edits.setdefault(last_user, []).append((args.get("old_str", ""), args.get("new_str", "")))
    out = list(msgs)
    for i, pairs in edits.items():
        lines = "\n".join(f'- "{_clip(old)}" -> "{_clip(new)}"' for old, new in pairs)
        note = ("<system-reminder>Because of the user message below, you amended your previous reply in place:\n"
                f"{lines}\nThe reply above already shows the corrected text; the user's message refers to the original wording."
                "</system-reminder>\n\n")
        content = out[i]["content"]
        new = note + content if isinstance(content, str) else [{"type": "text", "text": note}, *content]
        out[i] = {**out[i], "content": new}
    return out


def same_content(a, b) -> bool:
    """Stored row vs live reply; the trimmer rstrips live string content in place."""
    if isinstance(a, str) and isinstance(b, str):
        return a.rstrip() == b.rstrip()
    return a == b
