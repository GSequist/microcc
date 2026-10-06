import datetime
import json
import os
import time

from micro_cc.utils.tool_safety_ import is_read_only


def log_dropped_message(source: str, project_dir: str, count: int) -> None:
    """Append a line to ~/.micro-cc/dropped_messages.log (print is a no-op in the TUI)."""
    try:
        log_path = os.path.expanduser("~/.micro-cc/dropped_messages.log")
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        with open(log_path, "a") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} [{source}] dropped {count} malformed message(s) — {project_dir}\n")
    except OSError:
        pass


def _strip_image_content(content):
    """Replace image blocks with a text placeholder, recursing into tool_result; images never reach disk."""
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
    """JSON-safe {role, ts, content}: keeps thinking blocks (thinking, signature, id), replaces images with a placeholder."""
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
                        # id: the OpenAI Responses reasoning item id, needed to replay it; empty for Anthropic, which uses signature.
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
    """True if a stored row can reach the API: a dict, role user/assistant, non-empty str or list of typed dict blocks."""
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

_READ_ONLY_NOTE = "It is read-only: call it again if you still need the result."
_MAY_HAVE_RUN_NOTE = (
    "It may have partly run. Check the current state (re-read the file or "
    "inbox it touched) before repeating it, and do not assume it succeeded or failed."
)
_ARGS_SHOWN = 120


def _interrupted_text(block: dict) -> str:
    """REPAIR_RESULT_TEXT plus the call's name and args and how to treat it."""
    from micro_cc.tools.use_tool_ import resolve_call  # lazy: utils must not import tools at load

    name, args = resolve_call(block)
    if not name:
        return REPAIR_RESULT_TEXT
    try:
        shown = json.dumps(args, ensure_ascii=False)
    except (TypeError, ValueError):
        shown = str(args)
    if len(shown) > _ARGS_SHOWN:
        shown = shown[:_ARGS_SHOWN] + "…"
    note = _READ_ONLY_NOTE if is_read_only(name, args if isinstance(args, dict) else {}) else _MAY_HAVE_RUN_NOTE
    return f"{REPAIR_RESULT_TEXT} Call: {name}({shown}). {note}"


def cut_short_calls(msgs: list) -> list:
    """Inner names of the calls the repair answered at the tail of msgs (a run cut short), else []."""
    from micro_cc.tools.use_tool_ import resolve_call

    if len(msgs) < 2:
        return []
    last, prev = msgs[-1], msgs[-2]
    last_content, prev_content = last.get("content"), prev.get("content")
    if last.get("role") != "user" or prev.get("role") != "assistant":
        return []
    if not isinstance(last_content, list) or not isinstance(prev_content, list):
        return []
    if not all(isinstance(b, dict) and b.get("type") == "tool_result" for b in last_content):
        return []
    cut = {
        b.get("tool_use_id") for b in last_content
        if isinstance(b.get("content"), str) and b["content"].startswith(REPAIR_RESULT_TEXT)
    }
    return [
        resolve_call(b)[0] for b in prev_content
        if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("id") in cut
    ]


def repair_dangling_tool_use(msgs: list) -> list:
    """Answer every assistant tool_use that has no tool_result. A dangling last message gets a new tail message; a mid-history one is merged into the next user message so positions stay put. Load-time only."""
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
        uses = {b["id"]: b for b in content if isinstance(b, dict) and b.get("type") == "tool_use"}
        synthetic = [
            {
                "type": "tool_result",
                "tool_use_id": tid,
                "content": _interrupted_text(uses[tid]),
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
