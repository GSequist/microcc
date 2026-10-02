import hashlib
import json
import time

# 'input' is a snapshot of the last call; 'total_input' is the running sum across calls.
token_stats = {"input": 0, "output": 0, "total_input": 0, "trimmed": 0, "max": 0}


def save_token_stats(project_dir: str) -> None:
    """Persist token_stats to disk or Postgres."""
    from micro_cc.utils.msg_store_ import _get_storage_dir, _use_postgres

    if _use_postgres():
        from micro_cc.postgres_store import pg_store_
        try:
            pg_store_.save_token_stats(project_dir, token_stats)
        except Exception:
            pass
        return

    try:
        path = _get_storage_dir(project_dir) / "tokens.json"
        path.write_text(json.dumps(token_stats))
    except Exception:
        pass


def load_token_stats(project_dir: str) -> None:
    """Load token_stats from disk."""
    from micro_cc.utils.msg_store_ import _get_storage_dir, _use_postgres

    if _use_postgres():
        from micro_cc.postgres_store import pg_store_
        try:
            data = pg_store_.load_token_stats(project_dir)
            if data:
                token_stats.update({k: data[k] for k in token_stats if k in data})
        except Exception:
            pass
        return

    try:
        path = _get_storage_dir(project_dir) / "tokens.json"
        data = json.loads(path.read_text())
        token_stats.update({k: data[k] for k in token_stats if k in data})
    except Exception:
        pass


def peek_token_stats(project_dir: str) -> dict | None:
    """Read another project's persisted token stats without mutating globals."""
    from micro_cc.utils.msg_store_ import _get_storage_dir, _use_postgres

    if _use_postgres():
        from micro_cc.postgres_store import pg_store_
        try:
            return pg_store_.load_token_stats(project_dir)
        except Exception:
            return None

    try:
        path = _get_storage_dir(project_dir) / "tokens.json"
        return json.loads(path.read_text())
    except Exception:
        return None


def erase_token_stats(project_dir: str) -> None:
    """Zero and delete persisted token_stats."""
    from micro_cc.utils.msg_store_ import _get_storage_dir, _use_postgres

    token_stats.update(input=0, output=0, total_input=0, trimmed=0, max=0)

    if _use_postgres():
        from micro_cc.postgres_store import pg_store_
        try:
            pg_store_.save_token_stats(project_dir, token_stats)
        except Exception:
            pass
        return

    try:
        path = _get_storage_dir(project_dir) / "tokens.json"
        if path.exists():
            path.unlink()
    except Exception:
        pass


# Anthropic's default cache TTL: an idle gap past it likely explains a miss.
CACHE_TTL_S = 5 * 60
# Misses below this are cache-breakpoint granularity, not drift.
_CACHE_MISS_FLOOR = 5_000
# Not persisted: after a restart the next miss goes undetected once.
_last_call_time: float | None = None
_last_model: str | None = None
_segment_hashes: list[dict] = []


def _hash(obj) -> str:
    return hashlib.sha1(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()[:12]


def record_prompt_segments(tools: list, system_msgs: list) -> None:
    _segment_hashes.append({"tools": _hash(tools), "system": _hash(system_msgs)})
    del _segment_hashes[:-2]


def detect_cache_miss(usage: dict, model: str) -> dict | None:
    """Detect cache miss by comparing API usage against previous call."""
    global _last_call_time, _last_model

    now = time.monotonic()
    prev_time, prev_model = _last_call_time, _last_model
    prev_input = token_stats.get("input", 0)
    this_input = usage.get("input", 0)
    cache_read = usage.get("cache_read")
    _last_call_time = now
    _last_model = model

    if cache_read is None or prev_time is None or prev_input <= 0 or this_input <= 0:
        return None

    missed_tokens = min(prev_input, this_input) - cache_read
    if missed_tokens <= _CACHE_MISS_FLOOR:
        return None

    changed = []
    if len(_segment_hashes) == 2:
        before, after = _segment_hashes
        changed = [seg for seg in ("tools", "system") if before[seg] != after[seg]]

    return {
        "missed_tokens": missed_tokens,
        "idle_s": max(0.0, now - prev_time),
        "model_changed": prev_model is not None and prev_model != model,
        "changed_segments": changed,
    }


CHARS_PER_TOKEN = 3


def _approx_tokens(text: str) -> int:
    """Estimate tokens as char_count / 3."""
    return len(text) // CHARS_PER_TOKEN


def _cap_user_msg(msg: dict, cap: int) -> None:
    """Truncate oversized user message to cap tokens."""
    if msg.get("role") != "user":
        return
    content = msg.get("content")
    if not isinstance(content, str):
        return
    if _approx_tokens(content) <= cap:
        return
    msg["content"] = content[: cap * CHARS_PER_TOKEN] + "\n\n[... truncated ...]"


_IMAGE_TOKEN_ESTIMATE = 1500


def _content_for_token_count(content):
    """Replace image blocks with placeholders; return (content, image_count)."""
    if not isinstance(content, list):
        return content, 0
    out = []
    image_count = 0
    for block in content:
        if not isinstance(block, dict):
            out.append(block)
        elif block.get("type") == "image":
            out.append({"type": "text", "text": "[image]"})
            image_count += 1
        elif block.get("type") == "tool_result" and isinstance(block.get("content"), list):
            stripped, n = _content_for_token_count(block["content"])
            out.append({**block, "content": stripped})
            image_count += n
        else:
            out.append(block)
    return out, image_count


def _msg_tokens(msg: dict) -> int:
    """Approx token count for one message."""
    content = msg.get("content")
    image_count = 0
    if isinstance(content, list):
        content, image_count = _content_for_token_count(content)
    if isinstance(content, (dict, list)):
        content = json.dumps(content)
    return _approx_tokens(content or "") + image_count * _IMAGE_TOKEN_ESTIMATE


def total_tokens(messages: list[dict]) -> int:
    """Sum of _msg_tokens across `messages` — used by claude_loop_'s
    token-pressure trigger to decide when to fire a background checkpoint
    compaction, independent of whether token_cutter ends up trimming
    anything this turn."""
    return sum(_msg_tokens(m) for m in messages)


def _drop_orphaned_tool_blocks(messages: list[dict]) -> list[dict]:
    """Drop unmatched tool_use/tool_result blocks."""
    tool_use_ids = set()
    tool_result_ids = set()
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    if block.get("type") == "tool_use":
                        tool_use_ids.add(block.get("id"))
                    elif block.get("type") == "tool_result":
                        tool_result_ids.add(block.get("tool_use_id"))

    validated = []
    for msg in messages:
        content = msg.get("content")

        if isinstance(content, list):
            kept_blocks = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    if block.get("id") not in tool_result_ids:
                        continue  # Orphaned tool_use
                elif isinstance(block, dict) and block.get("type") == "tool_result":
                    if block.get("tool_use_id") not in tool_use_ids:
                        continue  # Orphaned tool_result
                kept_blocks.append(block)

            if not kept_blocks:
                continue  # nothing left of this turn
            msg["content"] = kept_blocks

        # Strip trailing whitespace from string content
        elif isinstance(content, str):
            msg["content"] = content.rstrip()

        validated.append(msg)

    return validated


def token_cutter(
    messages: list[dict],
    max_tokens: int,
    start_index: int = 0,
    checkpoint_summary: str = "",
    folded_tokens: int = 0,
) -> list[dict]:
    """Trim messages to max_tokens, keeping most recent suffix."""
    if not messages:
        return messages

    messages = messages[start_index:]
    # Gate on start_index, not on the summary being non-empty: an empty checkpoint still needs a stand-in.
    if start_index > 0:
        stand_in_text = (
            checkpoint_summary if checkpoint_summary
            else "[earlier conversation history was trimmed; no summary was captured for it]"
        )
        # Rebuilt every call, never stored in the checkpoint, so it can't drift; folded messages stay in storage.
        recovery_note = (
            f"The {start_index} messages this summary was folded from are still "
            "fully preserved, not lost — call search_history_(query=...) any time "
            "you need an exact quote, error message, file path, or tool output "
            "from before this summary instead of relying on its paraphrase."
        )
        messages = [{
            "role": "user",
            "content": (
                f"<conversation-summary>\n{stand_in_text}\n</conversation-summary>\n\n"
                f"<system-reminder>{recovery_note}</system-reminder>"
            ),
        }] + messages

    # The caller builds the real system prompt; drop stale system rows from old messages.jsonl.
    messages = [m for m in messages if m.get("role") != "system"]
    if not messages:
        return messages

    for msg in messages:
        _cap_user_msg(msg, max_tokens)

    total = sum(_msg_tokens(m) for m in messages)
    if total <= max_tokens:
        token_stats.update(trimmed=folded_tokens, max=max_tokens)
        # Validate even when nothing is trimmed: this is the last chokepoint before the API.
        return _drop_orphaned_tool_blocks(messages)

    tool_use_map = {}
    for msg in messages:
        if msg.get("role") == "assistant" and isinstance(msg.get("content"), list):
            for block in msg["content"]:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    tool_use_map[block.get("id")] = msg

    def paired_tool_use(msg):
        content = msg.get("content")
        if msg.get("role") == "user" and isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    return tool_use_map.get(block.get("tool_use_id"))
        return None

    def is_real_user_msg(msg):
        if msg.get("role") != "user":
            return False
        content = msg.get("content")
        if isinstance(content, list):
            return not any(
                isinstance(b, dict) and b.get("type") == "tool_result"
                for b in content
            )
        return "<system-reminder>" not in (content or "")

    # Always keep the last real user message so the model never loses its task.
    keep_ids = set()
    running = 0
    for msg in reversed(messages):
        if is_real_user_msg(msg):
            keep_ids.add(id(msg))
            running += _msg_tokens(msg)
            break

    kept_any = False
    for msg in reversed(messages):
        if id(msg) in keep_ids:
            kept_any = True
            continue  # already kept (pair partner or protected user query)
        tokens = _msg_tokens(msg)

        # A tool_result requires its tool_use — count and keep them together.
        pair = paired_tool_use(msg)
        pair_tokens = _msg_tokens(pair) if pair and id(pair) not in keep_ids else 0

        if kept_any and running + tokens + pair_tokens > max_tokens:
            break
        running += tokens + pair_tokens
        kept_any = True
        keep_ids.add(id(msg))
        if pair is not None:
            keep_ids.add(id(pair))

    # Phase 2: rebuild in original chronological order.
    result = [m for m in messages if id(m) in keep_ids]

    # Phase 3: validate tool pairs — drop orphans left at the cut boundary.
    validated = _drop_orphaned_tool_blocks(result)

    # Prepend truncation notice if we dropped anything
    if len(validated) < len(messages):
        validated.insert(0, {
            "role": "user",
            "content": "[Earlier conversation history has been truncated.]",
        })

    # folded_tokens (already not visible, replaced by the checkpoint
    # summary) plus whatever this call's own tail-cut additionally dropped
    # from the live pool.
    token_stats.update(
        trimmed=folded_tokens + (total - sum(_msg_tokens(m) for m in validated)),
        max=max_tokens,
    )
    return validated
