import asyncio
import datetime
import json
import os
import threading
from pathlib import Path
from typing import Callable, Optional
from micro_cc.models import model_call
from micro_cc.utils.helpers import project_hash, effective_trim_budget
from micro_cc.models.registry import compaction_trigger_for, summary_cap_for
from micro_cc.utils.tokenization_simple import CHARS_PER_TOKEN, _approx_tokens, _msg_tokens, total_tokens, _drop_orphaned_tool_blocks, token_stats, save_token_stats
from micro_cc.utils.msg_normalize_ import normalize_message, reconstruct_message, repair_dangling_tool_use, is_valid_message, log_dropped_message
from micro_cc.postgres_store import pg_store_


def _use_postgres() -> bool:
    """MICRO_CC_POSTGRES_URL set: every call here goes to Postgres instead of local disk."""
    return bool(os.getenv("MICRO_CC_POSTGRES_URL"))


# Additive relay after a local write, e.g. for an embedding app's own
# durable store — fires (project_dir, msgs), errors logged not raised.
_sink: Optional[Callable[[str, list], None]] = None


def set_sink(fn) -> None:
    """Register a post-local-write callback. None to unregister."""
    global _sink
    _sink = fn


def _notify_sink(project_dir: str, msgs: list) -> None:
    if _sink is None:
        return
    try:
        _sink(project_dir, msgs)
    except Exception as e:
        print(f"[msg_store_] sink failed for {project_dir}: {e}", flush=True)


def _get_storage_dir(project_dir: str) -> Path:
    """~/.micro-cc/projects/{name}_{hash}/ for this project."""
    normalized = os.path.abspath(os.path.expanduser(project_dir))
    path_hash = project_hash(project_dir)

    folder_name = os.path.basename(normalized) or "root"
    safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in folder_name)

    storage_dir = Path.home() / ".micro-cc" / "projects" / f"{safe_name}_{path_hash}"
    storage_dir.mkdir(parents=True, exist_ok=True)

    mapping_file = storage_dir / "project_path.txt"
    if not mapping_file.exists():
        mapping_file.write_text(normalized)

    return storage_dir


# Messages already flushed per project; store_msgs appends only msgs[known:].
_persisted_len: dict[str, int] = {}

# One RLock per project_dir: the TUI loop and the /gui uvicorn thread share this state.
_locks: dict[str, threading.RLock] = {}
_locks_guard = threading.Lock()


def _get_lock(project_dir: str) -> threading.RLock:
    with _locks_guard:
        lock = _locks.get(project_dir)
        if lock is None:
            lock = _locks[project_dir] = threading.RLock()
        return lock


def _rewrite_all(project_dir: str, msgs: list) -> None:
    """Overwrite messages.jsonl with all msgs. No repair here, that only runs at load. Caller holds the lock."""
    storage_dir = _get_storage_dir(project_dir)
    jsonl_path = storage_dir / "messages.jsonl"

    normalized = [normalize_message(msg) for msg in msgs]

    with open(jsonl_path, "w") as f:
        for msg in normalized:
            f.write(json.dumps(msg) + "\n")

    _persisted_len[project_dir] = len(msgs)


def hydrate_local_msgs(project_dir: str, msgs: list) -> None:
    """Seed local jsonl from an embedding app's durable copy. Does not notify the sink."""
    with _get_lock(project_dir):
        _rewrite_all(project_dir, msgs)


def store_msgs(project_dir: str, msgs: list) -> None:
    """Persist msgs: append the unflushed tail locally (full rewrite if the mark is stale), or insert into Postgres."""
    if _use_postgres():
        return pg_store_.store_msgs(project_dir, msgs)

    # Sink notified after the lock is released, not while held.
    wrote = False
    with _get_lock(project_dir):
        known = _persisted_len.setdefault(project_dir, 0)
        if known > len(msgs):
            _rewrite_all(project_dir, msgs)
            wrote = True
        else:
            new_tail = msgs[known:]
            if new_tail:
                storage_dir = _get_storage_dir(project_dir)
                jsonl_path = storage_dir / "messages.jsonl"

                with open(jsonl_path, "a") as f:
                    for msg in new_tail:
                        f.write(json.dumps(normalize_message(msg)) + "\n")

                _persisted_len[project_dir] = len(msgs)
                wrote = True

    if wrote:
        _notify_sink(project_dir, msgs)


def load_msgs(project_dir: str) -> list:
    """All stored messages for the project, repaired and reconstructed (local jsonl or Postgres)."""
    if _use_postgres():
        return pg_store_.load_msgs(project_dir)

    storage_dir = _get_storage_dir(project_dir)
    jsonl_path = storage_dir / "messages.jsonl"

    with _get_lock(project_dir):
        if not jsonl_path.exists():
            # store_msgs needs the mark set even for a new project.
            _persisted_len[project_dir] = 0
            return []

        msgs = []
        with open(jsonl_path, "r") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        msgs.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue  # Skip malformed lines

        valid_msgs = [m for m in msgs if is_valid_message(m)]
        if len(valid_msgs) != len(msgs):
            log_dropped_message("msg_store_", project_dir, len(msgs) - len(valid_msgs))
        msgs = valid_msgs

        # Mark = pre-repair count of valid rows, so repair's synthetic messages count as new tail.
        # Malformed lines stay on disk and are filtered on every load.
        _persisted_len[project_dir] = len(msgs)

    msgs = repair_dangling_tool_use(msgs)
    return [reconstruct_message(m) for m in msgs]


def rewind_msgs(project_dir: str, cut_idx: int) -> list:
    """Keep the messages before cut_idx (an index into load_msgs order), rewrite storage and return them."""
    if _use_postgres():
        kept = pg_store_.rewind_msgs(project_dir, cut_idx)
    else:
        kept = _rewind_msgs_local(project_dir, cut_idx)
        _notify_sink(project_dir, kept)

    # A checkpoint cut from the removed tail is orphaned: erase it.
    checkpoint = load_checkpoint(project_dir)
    if checkpoint and checkpoint["as_of_index"] > len(kept):
        erase_checkpoint(project_dir)

    return kept


def _rewind_msgs_local(project_dir: str, cut_idx: int) -> list:
    storage_dir = _get_storage_dir(project_dir)
    jsonl_path = storage_dir / "messages.jsonl"

    with _get_lock(project_dir):
        if not jsonl_path.exists():
            return []

        all_msgs = []
        with open(jsonl_path, "r") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        all_msgs.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue

        if not 0 <= cut_idx < len(all_msgs):
            return [reconstruct_message(m) for m in all_msgs]

        kept = repair_dangling_tool_use(all_msgs[:cut_idx])

        # Normalize like every other write path.
        with open(jsonl_path, "w") as f:
            for msg in kept:
                f.write(json.dumps(normalize_message(msg)) + "\n")

        _persisted_len[project_dir] = len(kept)

    return [reconstruct_message(m) for m in kept]


def erase_msgs(project_dir: str) -> None:
    """Clear conversation history for project."""
    if _use_postgres():
        return pg_store_.erase_msgs(project_dir)

    storage_dir = _get_storage_dir(project_dir)
    jsonl_path = storage_dir / "messages.jsonl"

    with _get_lock(project_dir):
        if jsonl_path.exists():
            jsonl_path.unlink()

        # Reset the mark so a fresh list is appended whole.
        _persisted_len[project_dir] = 0

    _notify_sink(project_dir, [])


# ---------------------------------------------------------------------------
# Conversation summary (sliding window)
# ---------------------------------------------------------------------------

def load_summary(project_dir: str) -> str:
    """Load conversation summary if it exists."""
    if _use_postgres():
        return pg_store_.load_summary(project_dir)

    storage_dir = _get_storage_dir(project_dir)
    summary_path = storage_dir / "summary.json"
    if not summary_path.exists():
        return ""
    try:
        data = json.loads(summary_path.read_text())
        return data.get("content", "")
    except (json.JSONDecodeError, KeyError):
        return ""


def _atomic_write_text(path: Path, text: str) -> None:
    """Write via a tmp file and os.replace so a reader or a crash never sees a torn file."""
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        tmp.write_text(text)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def load_checkpoint(project_dir: str) -> dict | None:
    """{content, as_of_index, folded_tokens} from summary.json or the Postgres row; None if absent or old-format."""
    if _use_postgres():
        return pg_store_.load_checkpoint(project_dir)

    storage_dir = _get_storage_dir(project_dir)
    summary_path = storage_dir / "summary.json"
    if not summary_path.exists():
        return None
    try:
        data = json.loads(summary_path.read_text())
    except json.JSONDecodeError:
        return None
    if "as_of_index" not in data:
        return None
    return {
        "content": data.get("content", ""),
        "as_of_index": data["as_of_index"],
        "folded_tokens": data.get("folded_tokens", 0),
    }


def store_checkpoint(project_dir: str, summary: str, as_of_index: int, folded_tokens: int = 0) -> None:
    """Write the checkpoint (summary.json or the Postgres row)."""
    if _use_postgres():
        return pg_store_.store_checkpoint(project_dir, summary, as_of_index, folded_tokens)

    storage_dir = _get_storage_dir(project_dir)
    summary_path = storage_dir / "summary.json"
    _atomic_write_text(summary_path, json.dumps({
        "content": summary,
        "as_of_index": as_of_index,
        "folded_tokens": folded_tokens,
        "ts": datetime.datetime.now().isoformat(),
    }))



# Projects with a compaction in flight.
_compacting: set[str] = set()

# Projects with a memory review in flight; a second one is skipped.
_reviewing: set[str] = set()


async def compact_checkpoint(project_dir: str, msgs: list, as_of_index: int, model: str) -> None:
    """Fold msgs[checkpoint:as_of_index] into the checkpoint summary once the backlog passes the trigger. Fire-and-forget, applies next turn."""
    if project_dir in _compacting:
        return
    checkpoint = load_checkpoint(project_dir)
    old_index = checkpoint["as_of_index"] if checkpoint else 0
    if as_of_index <= old_index:
        return

    budget = effective_trim_budget(model)
    if total_tokens(msgs[old_index:as_of_index]) < compaction_trigger_for(model, budget):
        return

    _compacting.add(project_dir)
    try:
        prev_summary = checkpoint["content"] if checkpoint else load_summary(project_dir)
        summary_cap = summary_cap_for(model, budget)

        # Cap the fold to the budget; running becomes folded_tokens.
        fold_budget = budget - _approx_tokens(prev_summary)
        cutoff = old_index
        running = 0
        for i in range(old_index, as_of_index):
            running += _msg_tokens(msgs[i])
            if running > fold_budget and cutoff > old_index:
                break
            cutoff = i + 1
        as_of_index = cutoff
        prev_folded_tokens = checkpoint["folded_tokens"] if checkpoint else 0
        folded_tokens = prev_folded_tokens + running

        to_fold = _drop_orphaned_tool_blocks(
            [m for m in msgs[old_index:as_of_index] if m.get("role") != "system"]
        )
        if not to_fold:
            store_checkpoint(project_dir, prev_summary, as_of_index, folded_tokens)
            return

        new_summary = await _call_summary_model(prev_summary, to_fold, model, summary_cap, project_dir)
        if new_summary is None:
            # Failed: leave as_of_index so the next turn retries.
            return
        store_checkpoint(project_dir, new_summary, as_of_index, folded_tokens)
        # Own task: awaiting it would hold _compacting for as long as the review runs.
        asyncio.create_task(_review_memory(new_summary, model, project_dir))
    finally:
        _compacting.discard(project_dir)


_SEARCH_SNIPPET_RADIUS = 160  # chars of context kept on each side of a match


def _flatten_content_for_search(content) -> str:
    """Text of a message's content (text, thinking, tool_use input, tool_result) without image bytes."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            parts.append(block.get("text", ""))
        elif btype == "thinking":
            parts.append(block.get("thinking", ""))
        elif btype == "tool_use":
            parts.append(f"{block.get('name', '')} {json.dumps(block.get('input', {}))}")
        elif btype == "tool_result":
            inner = block.get("content", "")
            parts.append(inner if isinstance(inner, str) else _flatten_content_for_search(inner))
    return "\n".join(p for p in parts if p)


def _snippet(text: str, query: str) -> str:
    """A window of text around the first case-insensitive hit of query."""
    low = text.lower()
    i = low.find(query.lower())
    if i == -1:
        return text[: _SEARCH_SNIPPET_RADIUS * 2]
    start = max(0, i - _SEARCH_SNIPPET_RADIUS)
    end = min(len(text), i + len(query) + _SEARCH_SNIPPET_RADIUS)
    return ("…" if start > 0 else "") + text[start:end] + ("…" if end < len(text) else "")


def search_msgs(project_dir: str, query: str, limit: int = 10) -> list[dict]:
    """Case-insensitive substring search over every stored message, newest first: [{index, role, snippet}]."""
    if _use_postgres():
        return pg_store_.search_msgs(project_dir, query, limit)

    if not query:
        return []

    storage_dir = _get_storage_dir(project_dir)
    jsonl_path = storage_dir / "messages.jsonl"
    if not jsonl_path.exists():
        return []

    with _get_lock(project_dir):
        with open(jsonl_path, "r") as f:
            lines = f.readlines()

    q = query.lower()
    matches = []
    for idx, line in enumerate(lines):
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        text = _flatten_content_for_search(msg.get("content"))
        if q in text.lower():
            matches.append({"index": idx, "role": msg.get("role", ""), "snippet": _snippet(text, query)})

    matches.reverse()
    return matches[:limit]


def erase_summary(project_dir: str) -> None:
    """Delete conversation summary."""
    if _use_postgres():
        return pg_store_.erase_summary(project_dir)

    storage_dir = _get_storage_dir(project_dir)
    summary_path = storage_dir / "summary.json"
    if summary_path.exists():
        summary_path.unlink()


def _strip_images_for_summary(content):
    """Replace base64 image blocks with a text placeholder before summarizing."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return content
    out = []
    for block in content:
        if not isinstance(block, dict):
            out.append(block)
        elif block.get("type") == "image":
            out.append({"type": "text", "text": "[image omitted from summary]"})
        elif block.get("type") == "tool_result" and isinstance(block.get("content"), list):
            out.append({**block, "content": _strip_images_for_summary(block["content"])})
        else:
            out.append(block)
    return out


def erase_checkpoint(project_dir: str) -> None:
    """Same slot as the summary; separate name so /clear reads as erasing both."""
    erase_summary(project_dir)


_MEMORY_REVIEW_MAX_ROUNDS = 8


def _store_memory_review_recap(project_dir: str, recap: str) -> None:
    """Marker file the TUI polls to flash a memory-review recap (local only)."""
    storage_dir = _get_storage_dir(project_dir)
    (storage_dir / "memory_review.json").write_text(json.dumps({
        "recap": recap,
        "ts": datetime.datetime.now().isoformat(),
    }))


def load_memory_review_recap(project_dir: str) -> dict | None:
    """{recap, ts} last written by the memory review, or None."""
    storage_dir = _get_storage_dir(project_dir)
    path = storage_dir / "memory_review.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return None


async def _review_memory(new_summary: str, model: str, project_dir: str) -> None:
    """After a real fold, let the model add/edit/delete memory entries from the new summary (bounded rounds, own task, swallows errors)."""
    if project_dir in _reviewing:
        return
    _reviewing.add(project_dir)
    try:
        from micro_cc.models.schema import function_to_schema
        from micro_cc.tools.memory_tool_ import memory_
        from micro_cc.utils import memory_store_

        manifest_parts = []
        global_items = memory_store_.list_memories()
        if global_items:
            manifest_parts.append(
                "Global (all projects):\n"
                + "\n".join(f"- {it['key']} — {it['description']}" for it in global_items)
            )
        project_items = memory_store_.list_memories(project_dir=project_dir)
        if project_items:
            manifest_parts.append(
                "Project (this project_dir only):\n"
                + "\n".join(f"- {it['key']} — {it['description']}" for it in project_items)
            )
        manifest = "\n\n".join(manifest_parts) if manifest_parts else "(empty — no entries in either scope yet)"

        instructions = (
            "A chunk of conversation was just folded into a checkpoint summary "
            "(below). Review it against your existing memory manifest (also "
            "below) and decide: add a new durable entry, edit an existing one "
            "that's now stale or incomplete, delete one now contradicted, or do "
            "nothing if there's nothing here worth keeping long-term. Use the "
            "memory_ tool for any of those — scope='global' for facts that hold "
            "regardless of project, scope='project' for facts specific to this "
            "codebase. Call memory_(action='get', ...) first if you need an "
            "existing entry's full content before editing it — you can call "
            "memory_ several times in the same turn (e.g. get two different "
            "keys at once) rather than one call per turn. When you're done "
            "(including doing nothing), reply with a short confirmation and no "
            "further tool calls.\n\n"
            f"<memory-manifest>\n{manifest}\n</memory-manifest>\n\n"
            f"<newly-folded-summary>\n{new_summary}\n</newly-folded-summary>"
        )

        input_msgs = [{"role": "user", "content": instructions}]
        tools = [function_to_schema(memory_)]

        # '+ key' / '~ key' / '- key' per applied add/edit/delete, kept across rounds.
        change_log: list[str] = []
        _APPLIED_PREFIX = {
            "add": ("+", "Added memory entry"),
            "edit": ("~", "Updated memory entry"),
            "delete": ("-", "Deleted memory entry"),
        }

        for _ in range(_MEMORY_REVIEW_MAX_ROUNDS):
            resp = await model_call(input_msgs, model, max_tokens=1024, tools=tools)
            if not resp:
                break
            # Count this call's spend in token_stats.
            in_tok, out_tok = resp.usage.get("input", 0), resp.usage.get("output", 0)
            if in_tok or out_tok:
                token_stats["total_input"] += in_tok
                token_stats["output"] += out_tok
                save_token_stats(project_dir)
            if not resp.content:
                break
            tool_blocks = [b for b in resp.content if getattr(b, "type", None) == "tool_use"]
            if not tool_blocks:
                break

            input_msgs.append({
                "role": "assistant",
                "content": [{"type": "tool_use", "id": b.id, "name": b.name, "input": b.input} for b in tool_blocks],
            })

            tool_result_blocks = []
            for b in tool_blocks:
                if b.name != "memory_":
                    result = f"Error: unexpected tool '{b.name}' in memory review."
                else:
                    result = await memory_(**b.input, project_dir=project_dir)
                    marker = _APPLIED_PREFIX.get(b.input.get("action"))
                    key = (b.input.get("key") or "").strip()
                    if marker and key and result.startswith(marker[1]):
                        trimmed = key if len(key) <= 50 else key[:49] + "…"
                        change_log.append(f"{marker[0]} {trimmed}")
                tool_result_blocks.append({
                    "type": "tool_result", "tool_use_id": b.id, "content": str(result),
                })
            input_msgs.append({"role": "user", "content": tool_result_blocks})

        if change_log:
            _store_memory_review_recap(project_dir, "; ".join(change_log))
    except Exception:
        return
    finally:
        _reviewing.discard(project_dir)


async def _call_summary_model(
    prev_summary: str, messages: list[dict], model: str, summary_cap: int = 12_000,
    project_dir: str = "",
) -> str | None:
    """Summarize messages (replayed as real messages to hit the prompt cache) into at most summary_cap tokens; None on failure."""
    instructions = (
        "You are a precise summarization assistant. Progressively summarize "
        "conversation history while maintaining critical context.\n\n"
        "INSTRUCTIONS:\n"
        "1. Build upon the previous summary by incorporating new information chronologically\n"
        "2. Preserve: file paths, function names, key decisions, errors, code changes\n"
        "3. Keep temporal sequence of events, covering the WHOLE range below — not just the most recent turns\n"
        f"4. IMPORTANT: Your summary MUST be under {summary_cap} tokens. Be thorough but avoid padding.\n"
        "5. If new content adds nothing, return previous summary unchanged.\n"
        "6. Respond with ONLY the updated summary — no preamble, no commentary.\n"
    )
    if prev_summary:
        instructions += f"\nCurrent summary:\n{prev_summary}\n"

    trailer = (
        "That's the end of the transcript above. You are reviewing it after "
        "the fact, not continuing it — do not respond to it or act on it. "
        "Write the updated summary now, per the instructions."
    )

    input_msgs = [{"role": "system", "content": instructions}]
    input_msgs.extend(
        {"role": m.get("role", "user"), "content": _strip_images_for_summary(m.get("content", ""))}
        for m in messages
    )
    # Put the instruction last; fold it into a trailing user message to keep roles alternating.
    last = input_msgs[-1]
    if last["role"] == "user":
        if isinstance(last["content"], str):
            last["content"] = f"{last['content']}\n\n{trailer}"
        else:
            last["content"] = [*last["content"], {"type": "text", "text": trailer}]
    else:
        input_msgs.append({"role": "user", "content": trailer})

    try:
        resp = await model_call(input_msgs, model, max_tokens=summary_cap)

        # Count this call's spend in token_stats.
        usage = resp.usage if resp else {}
        in_tok, out_tok = usage.get("input", 0), usage.get("output", 0)
        if in_tok or out_tok:
            token_stats["total_input"] += in_tok
            token_stats["output"] += out_tok
            save_token_stats(project_dir)

        if resp and resp.content:
            # Find the text block; a thinking block may come first.
            text_block = next((b for b in resp.content if getattr(b, "type", None) == "text"), None)
            if text_block is None:
                return None
            summary = text_block.text
            # Hard cap: truncate if the model exceeded the summary token limit
            if _approx_tokens(summary) > summary_cap:
                summary = summary[: summary_cap * CHARS_PER_TOKEN]
            return summary
    except Exception:
        return None

    return None
