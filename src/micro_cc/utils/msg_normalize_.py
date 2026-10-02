import datetime
import os
import time


def log_dropped_message(source: str, project_dir: str, count: int) -> None:
    """Durable trace for load_msgs dropping malformed rows (see
    is_valid_message) — NOT a bare print(). start_live_tui_.py monkeypatches
    builtins.print to a no-op for the whole process (raw stdout would
    corrupt raw-terminal rendering — see its own comment), so a plain print
    here would be silently swallowed in exactly the surface (the TUI) this
    is meant to be diagnosable from. Same os.makedirs + try/except OSError
    pattern as stack_.py's _log_render_error / render_errors.log — see
    notes/logging.md."""
    try:
        log_path = os.path.expanduser("~/.micro-cc/dropped_messages.log")
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        with open(log_path, "a") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} [{source}] dropped {count} malformed message(s) — {project_dir}\n")
    except OSError:
        pass


def _strip_image_content(content):
    """Replace {"type": "image", ...} blocks with a text placeholder,
    recursing into tool_result wrappers — everything else passes through
    untouched. Images must never reach messages.jsonl: they stay in the
    in-memory msgs list claude_loop_.py holds for the running session's
    own API calls (that's the only place they need to exist), but never
    get written to disk. Two concrete failures from actually persisting
    them: (1) a raw base64 blob run through tokenization_simple's
    len(text)//3 char-count heuristic registers as enormous — one
    unresized screenshot as over a million "tokens" — blowing the trim
    budget and triggering checkpoint-fold/summarize on what's otherwise a
    tiny conversation; (2) confirmed directly against a live project's
    messages.jsonl: an 8MB file, individual lines up to 3.6MB, all real
    base64 JPEG data, in a 17-message session. Same shape/placeholder
    convention as msg_store_._strip_images_for_summary (a narrower fix
    for just the summarizer's own input) — this is the same idea applied
    at the actual persistence boundary, where it belongs."""
    if not isinstance(content, list):
        return content
    out = []
    for block in content:
        if not isinstance(block, dict):
            out.append(block)
        elif block.get("type") == "image":
            out.append({"type": "text", "text": "[image omitted from history]"})
        elif block.get("type") == "tool_result" and isinstance(block.get("content"), list):
            out.append({**block, "content": _strip_image_content(block["content"])})
        else:
            out.append(block)
    return out


def normalize_message(msg: dict) -> dict:
    """Convert message to serializable dict - strips thinking blocks (API rejects them)."""
    normalized = {
        "role": msg.get("role"),
        "ts": datetime.datetime.now().isoformat(),
    }

    content = msg.get("content")

    if isinstance(content, str):
        normalized["content"] = content

    elif isinstance(content, list):
        normalized["content"] = []
        for item in content:
            if hasattr(item, "type"):
                if item.type == "thinking":
                    normalized["content"].append({
                        "type": "thinking",
                        "thinking": item.thinking,
                        "signature": item.signature,
                        # OpenAI Responses path: the reasoning item's own id,
                        # required to replay it ahead of the function_call
                        # items it preceded (models/openai.py gates on
                        # block.get("id") and silently drops an id-less
                        # block). Dropping it here meant a reasoning item
                        # survived in memory but vanished from every
                        # persist->reload — resume, compaction, self-restart.
                        # "" for Anthropic, which identifies thinking by
                        # signature rather than id; harmless there.
                        "id": item.id,
                    })
                elif item.type == "tool_use":
                    normalized["content"].append({
                        "type": "tool_use",
                        "id": item.id,
                        "name": item.name,
                        "input": item.input
                    })
                elif item.type == "text":
                    normalized["content"].append({
                        "type": "text",
                        "text": item.text
                    })
                elif item.type == "tool_result":
                    normalized["content"].append({
                        "type": "tool_result",
                        "tool_use_id": item.tool_use_id,
                        "content": _strip_image_content(item.content),
                    })
            elif isinstance(item, dict):
                if item.get("type") == "image":
                    normalized["content"].append({"type": "text", "text": "[image omitted from history]"})
                elif item.get("type") == "tool_result" and isinstance(item.get("content"), list):
                    normalized["content"].append({**item, "content": _strip_image_content(item["content"])})
                else:
                    normalized["content"].append(item)

        if len(normalized["content"]) == 1 and normalized["content"][0].get("type") == "text":
            normalized["content"] = normalized["content"][0]["text"]

    return normalized


_VALID_ROLES = {"user", "assistant"}


def is_valid_message(msg) -> bool:
    """A stored row is well-formed enough to survive reconstruct_message and
    reach the API: a dict, role in {user, assistant} (anything else 400s),
    and non-empty content — a str or a list of dict blocks each carrying a
    "type" (an empty content list also 400s; Anthropic rejects both). Any
    on-disk/Postgres row that doesn't clear this bar gets dropped by
    load_msgs rather than forwarded and taking the whole session's next
    turn down with it."""
    if not isinstance(msg, dict):
        return False
    if msg.get("role") not in _VALID_ROLES:
        return False
    content = msg.get("content")
    if isinstance(content, str):
        return len(content) > 0
    if isinstance(content, list):
        return len(content) > 0 and all(isinstance(b, dict) and "type" in b for b in content)
    return False


def reconstruct_message(normalized: dict) -> dict:
    """Reconstruct API-compatible message from stored format."""
    return {
        "role": normalized["role"],
        "content": normalized.get("content", "")
    }


REPAIR_RESULT_TEXT = (
    "No result recorded — the run ended (process killed, crashed, or the turn "
    "was interrupted) before this tool finished."
)


def repair_dangling_tool_use(msgs: list) -> list:
    """Answer any assistant tool_use left without a tool_result.

    An interrupt landing between the assistant's tool_use being appended and
    its tool_result being appended (TUI: ESC mid tool-result loop; webui:
    /api/chat/stop -> gen.aclose() while resumed; a headless process killed
    or crashed mid-tool) can leave that tool_use dangling, and every later
    model call against the transcript would be rejected. Runs only at load
    time (see msg_store_._rewrite_all for why never at write time).

    Two shapes, chosen so the returned list lines up with what's on disk:
      - dangling tool_use is the LAST message: append one synthetic
        tool_result message. It's a genuinely new tail, so the next
        store_msgs appends it (the append mark is the pre-repair count).
      - dangling mid-history (followed by a user message, e.g. a prompt or a
        partial tool_result set): merge the missing results INTO that next
        user message instead of inserting a new one. Inserting shifted every
        later index by one, so store_msgs/pg COUNT appended the wrong
        message — the last one again — on every reload, and the synthetic
        result itself never reached disk. Merging keeps positions intact;
        the repair is simply re-applied on each load. (An assistant followed
        directly by another assistant still gets an inserted message — that
        transcript was already invalid.)

    The synthetic result carries the dangling tool_use's own timestamp, not
    load time, so a transcript reads as "died during this call" instead of
    looking like something happened at the moment of the reload.
    """
    repaired = []
    skip_next = False
    for i, msg in enumerate(msgs):
        if skip_next:
            skip_next = False
            continue
        repaired.append(msg)
        content = msg.get("content")
        if msg.get("role") != "assistant" or not isinstance(content, list):
            continue

        tool_use_ids = [
            b["id"] for b in content
            if isinstance(b, dict) and b.get("type") == "tool_use"
        ]
        if not tool_use_ids:
            continue

        next_msg = msgs[i + 1] if i + 1 < len(msgs) else None
        next_content = next_msg.get("content") if next_msg else None
        answered_ids = set()
        if next_msg and next_msg.get("role") == "user" and isinstance(next_content, list):
            answered_ids = {
                b.get("tool_use_id") for b in next_content
                if isinstance(b, dict) and b.get("type") == "tool_result"
            }

        missing = [tid for tid in tool_use_ids if tid not in answered_ids]
        if not missing:
            continue
        synthetic = [
            {
                "type": "tool_result",
                "tool_use_id": tid,
                "content": REPAIR_RESULT_TEXT,
                "is_error": True,
            }
            for tid in missing
        ]
        if next_msg is not None and next_msg.get("role") == "user":
            if isinstance(next_content, list):
                merged_content = synthetic + list(next_content)
            else:
                merged_content = synthetic + [{"type": "text", "text": next_content or ""}]
            repaired.append({**next_msg, "content": merged_content})
            skip_next = True
        else:
            repaired.append({
                "role": "user",
                "ts": msg.get("ts") or datetime.datetime.now().isoformat(),
                "content": synthetic,
            })
    return repaired
