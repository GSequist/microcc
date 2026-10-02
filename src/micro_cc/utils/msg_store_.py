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
    """The one toggle: set MICRO_CC_POSTGRES_URL and every call in this module
    routes to Postgres instead of local disk. Nothing upstream (claude_loop_,
    start_headless_) has to know or change — they only ever call
    load_msgs/store_msgs/etc. This is what makes conversation history
    survive Container Apps Jobs, whose local disk is wiped between runs."""
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
    """Get CC-style storage path: ~/.micro-cc/projects/{project_hash}/

    Hash ensures valid folder name regardless of project path characters.
    """
    # Normalize and hash the project path
    normalized = os.path.abspath(os.path.expanduser(project_dir))
    path_hash = project_hash(project_dir)

    # Human-readable prefix (last folder name)
    folder_name = os.path.basename(normalized) or "root"
    safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in folder_name)

    # Store in ~/.micro-cc/projects/ (works for both source and pip installs)
    storage_dir = Path.home() / ".micro-cc" / "projects" / f"{safe_name}_{path_hash}"
    storage_dir.mkdir(parents=True, exist_ok=True)

    # Store project path mapping for debugging
    mapping_file = storage_dir / "project_path.txt"
    if not mapping_file.exists():
        mapping_file.write_text(normalized)

    return storage_dir


# Per-project high-water mark: how many messages are already flushed to
# messages.jsonl. Lets store_msgs (below) write only the new tail instead
# of a full O(n) rewrite. Every function in this module that can create or
# replace a project's message list (load_msgs, store_msgs, rewind_msgs,
# erase_msgs) sets this itself in the same call, so store_msgs always
# finds it accurate for the list it's handed.
_persisted_len: dict[str, int] = {}

# One re-entrant lock per project_dir, guarding both messages.jsonl and the
# _persisted_len entry for that project together as a single critical
# section. Needed because two OS threads can legitimately touch the same
# project_dir concurrently in this process — the TUI's asyncio loop and the
# /gui uvicorn server both run here (webui/launch.py starts uvicorn on its
# own thread, deliberately not a subprocess, so the two share this module's
# state). Without a lock, two interleaved read-known/write-file/update-mark
# sequences can duplicate lines on disk. RLock (not Lock) because
# store_msgs's self-healing fallback calls _rewrite_all while already
# holding the lock for the same project_dir.
_locks: dict[str, threading.RLock] = {}
_locks_guard = threading.Lock()


def _get_lock(project_dir: str) -> threading.RLock:
    with _locks_guard:
        lock = _locks.get(project_dir)
        if lock is None:
            lock = _locks[project_dir] = threading.RLock()
        return lock


def _rewrite_all(project_dir: str, msgs: list) -> None:
    """Full O(n) rewrite of messages.jsonl — the always-correct fallback
    every other write path (store_msgs's self-heal, rewind_msgs) collapses
    to when it can't safely reason about a partial write. Overwrites the
    file with all messages, one JSON object per line.

    Deliberately does NOT run repair_dangling_tool_use — store_msgs calls
    this continuously mid-run (right after every tool_call event, before
    that tool has actually executed and produced its result), so the last
    message is routinely an unanswered tool_use for a brief, completely
    normal moment. Repairing here wrote a synthetic "Interrupted before
    completion" block to disk on every single tool call, transiently —
    invisible in the final file (the very next call, once the real
    tool_result lands, overwrites it clean again, since this is a full
    rewrite, not an append), but very visible to anything tailing the raw
    file live (microcc-watch), which caught it on effectively every tool
    call across a run and reported the transcript as "littered with
    interrupted" even though nothing was ever actually interrupted. The
    repair only belongs at LOAD time — see load_msgs/rewind_msgs — where
    "unanswered" really does mean "will never be answered," because we're
    about to resume this history and hand it back to the model.

    Caller must already hold _get_lock(project_dir).
    """
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
    """Persist the latest state durable for this project (or to Postgres,
    see _use_postgres) — the one function every caller uses, on every
    single tool_call/tool_result/final_text/done/error event, all the way
    down to per-keystroke-adjacent granularity. Named to match this
    module's git history and pg_store_.store_msgs, not "append", even
    though the local-disk fast path below is an append: callers shouldn't
    have to know or care which strategy is live underneath.

    Fast path: writes only the messages not yet flushed to disk for this
    project (O(new messages) instead of O(all messages) per call, which is
    what turns the per-tool-call persist into O(n) total over a session
    instead of O(n^2)). `known` defaults to 0 the first time a project_dir
    is seen — nothing persisted yet, so appending the whole list to a
    fresh/empty file (open(..., "a") creates it) is exactly correct, not a
    special case. Every function that can shrink or replace `msgs`
    (store_msgs itself, rewind_msgs, erase_msgs) updates the mark in the
    same call, and load_msgs seeds it to the exact on-disk count before
    handing back the list it read — so by the time any caller has a `msgs`
    to pass here, the mark should already be accurate for it.

    "Should" is doing real work in that sentence, so this doesn't just trust
    it: `known > len(msgs)` is the one state that turns the fast path
    actively harmful — a plain `msgs[known:]` slice past the end of a
    shorter-than-expected list silently returns `[]`, so a stale/wrong mark
    would drop the rest of this project's history forever with no error,
    ever. Rather than rely on every caller getting the invariant right
    forever, fall back to _rewrite_all (full rewrite) whenever that guard
    trips — self-healing back to the always-correct full-overwrite
    behavior instead of silently losing messages. The lock (see _get_lock)
    covers this same critical section against a second thread (the /gui
    uvicorn server, sharing this module's state with the TUI's asyncio loop
    — see webui/launch.py) racing the read-known/write/update-mark
    sequence.

    Relies on messages already counted in the mark never being mutated in
    place after being appended to `msgs` — true today: every caller
    (claude_loop_) fully builds a message's content before appending it,
    never edits an already-appended entry.
    """
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
    """Load all messages from JSONL file for project (or from Postgres, see _use_postgres)."""
    if _use_postgres():
        return pg_store_.load_msgs(project_dir)

    storage_dir = _get_storage_dir(project_dir)
    jsonl_path = storage_dir / "messages.jsonl"

    with _get_lock(project_dir):
        if not jsonl_path.exists():
            # Establish the mark even on a brand-new project: store_msgs
            # requires it to already be set, no exceptions — see its
            # docstring.
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

        # High-water mark for store_msgs: must equal len(msgs) here, i.e.
        # the list actually being returned (and that the caller will keep
        # appending to) — NOT the raw pre-filter line count. store_msgs's
        # fast path always appends msgs[known:] to disk; if the mark
        # overstated what's in this list (raw count, with malformed lines
        # dropped above), the next store_msgs call would slice past the
        # start of this turn's own newly-appended messages and silently
        # drop them from disk instead of just the malformed lines. The
        # malformed lines themselves stay on disk (harmless — load_msgs
        # filters them out every time) rather than being purged; that's the
        # trade made here, over risking real messages.
        #
        # Captured before repair_dangling_tool_use, which can insert
        # synthetic entries that don't exist on disk yet. Marking the
        # pre-repair count means those synthetic entries correctly look
        # like "new tail" to store_msgs's first call on the resumed list,
        # same as any other pending message.
        _persisted_len[project_dir] = len(msgs)

    msgs = repair_dangling_tool_use(msgs)
    return [reconstruct_message(m) for m in msgs]


def rewind_msgs(project_dir: str, cut_idx: int) -> list:
    """Rewind to just before the message at `cut_idx`.

    `cut_idx` indexes the JSONL exactly as load_msgs() returns it — one entry
    per line, same order — so the caller can hand back the index it built the
    picker from. It used to match on an 80-char content prefix and walk
    backwards for the last hit, which silently rewound to the wrong turn as
    soon as two prompts shared an opening (any repeated "continue", "fix
    that", or a re-submitted rewind); with the picker now showing far more
    history that stopped being an edge case.

    Keeps all messages up to (not including) `cut_idx`, rewrites the JSONL
    (or Postgres row, see _use_postgres) and returns the trimmed message list.
    """
    if _use_postgres():
        kept = pg_store_.rewind_msgs(project_dir, cut_idx)
    else:
        kept = _rewind_msgs_local(project_dir, cut_idx)
        _notify_sink(project_dir, kept)

    # A checkpoint's as_of_index is only meaningful relative to the message
    # list it was cut from — if this rewind just truncated history to
    # shorter than that index, the checkpoint is now orphaned. Left in
    # place, two things break: claude_loop_'s read-time guard treats it as
    # no-checkpoint (safe, but only papers over it), while
    # compact_checkpoint reads the SAME stale as_of_index directly from
    # storage and sees `len(msgs) <= old_index` forever — it can never
    # build a new checkpoint until history organically grows back past the
    # old index. Erasing here, for both backends, is what actually
    # unsticks it.
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

        # Read all messages
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

        # Keep everything before the selected message
        kept = repair_dangling_tool_use(all_msgs[:cut_idx])

        # Rewrite JSONL — normalize_message here too, same as store_msgs,
        # so a rewound file matches what every other write path produces
        # instead of re-persisting whatever raw shape happened to be on
        # disk (harmless today only because everything on disk was already
        # normalized when first written; kept explicit so it stays true
        # even if that stops holding).
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

        # Explicit clear, not just a no-op: a stale mark left at, say, 12
        # could coincidentally equal len(msgs) on some later store_msgs
        # call after a fresh conversation reaches 12 messages, and get
        # skipped as "already on disk" for a file that no longer exists.
        # Resetting to 0 makes the next store_msgs call treat the whole
        # (fresh) list as new tail, which recreates the file correctly via
        # the append path.
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


def _store_summary(project_dir: str, summary: str) -> None:
    """Persist conversation summary."""
    if _use_postgres():
        return pg_store_.store_summary(project_dir, summary)

    storage_dir = _get_storage_dir(project_dir)
    summary_path = storage_dir / "summary.json"
    summary_path.write_text(json.dumps({
        "content": summary,
        "ts": datetime.datetime.now().isoformat(),
    }))


def load_checkpoint(project_dir: str) -> dict | None:
    """New-format checkpoint reader: {"content": str, "as_of_index": int,
    "folded_tokens": int} — same summary.json/pg row load_summary reads,
    repurposed. Returns None for a missing file/row AND for an old-format
    one (no "as_of_index") — either way the caller falls back to
    load_summary's unconditional inject with no start_index passed to
    token_cutter, byte-identical to pre-checkpoint behavior. The first real
    compact_checkpoint call for a project overwrites the old-format entry
    with this shape; no separate migration step.

    "folded_tokens" defaults to 0 for a checkpoint written before this field
    existed — self-heals the moment compact_checkpoint next advances it, no
    migration needed; the only cost is token_cutter's `trimmed` stat
    under-reporting until then.
    """
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
    """Persist the new-format checkpoint — same slot _store_summary writes,
    repurposed with an as_of_index and folded_tokens (see load_checkpoint)."""
    if _use_postgres():
        return pg_store_.store_checkpoint(project_dir, summary, as_of_index, folded_tokens)

    storage_dir = _get_storage_dir(project_dir)
    summary_path = storage_dir / "summary.json"
    summary_path.write_text(json.dumps({
        "content": summary,
        "as_of_index": as_of_index,
        "folded_tokens": folded_tokens,
        "ts": datetime.datetime.now().isoformat(),
    }))


# Summarization uses the SAME model driving the main conversation (see
# compact_checkpoint's `model` param) — one budget to reason about, not the
# main model's and a separate fixed summarizer's. TRIGGER_RATIO/
# SUMMARY_CAP_FRACTION and the compaction_trigger_for/summary_cap_for
# helpers built on them now live in models/registry.py — the one place this
# rule is defined, shared verbatim across every repo that compacts this way
# (see the comment block above registry.trim_budget_for).

# Per-project in-flight guard — claude_loop_ fires this every loop
# iteration; without this a long turn would spawn overlapping summary calls.
_compacting: set[str] = set()

# Separate in-flight guard for _review_memory, below — it's deliberately NOT
# awaited inside compact_checkpoint's own _compacting section (see that
# function's call site), so without this a second compaction pass firing
# before a slow review finishes could start a second, overlapping review for
# the same project_dir. memory_store_'s local backend has no locking of its
# own (plain _load/_save, unlike msg_store_'s per-project RLock), so two
# concurrent reviews really could race on a read-modify-write. This just
# skips the second one rather than trying to serialize/queue it — a missed
# review this pass gets picked up by the next compaction anyway.
_reviewing: set[str] = set()


async def compact_checkpoint(project_dir: str, msgs: list, as_of_index: int, model: str) -> None:
    """Folds msgs[old_checkpoint.as_of_index:as_of_index] into the checkpoint
    summary and advances as_of_index, using `model` (the main conversation's
    active model) to do the folding. No-ops if the backlog since the last
    checkpoint isn't over TRIGGER_RATIO yet.

    Fire-and-forget — the turn that called this doesn't get a same-turn
    token reduction, it still falls back to plain token_cutter truncation.
    A new checkpoint only takes effect next turn.

    `msgs` is the caller's live, growing list — safe to slice without
    copying since store_msgs guarantees already-appended messages are never
    mutated in place.

    On a real fold (not the no-op paths above), also runs _review_memory
    right after store_checkpoint — the model gets one shot to add/edit/
    delete memory entries based on what just got folded, in the same
    background task, before this returns.
    """
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

        # Cap how much of the backlog goes into this one summarization
        # call — see registry.TRIGGER_RATIO's comment block. `running` ends up being
        # the real (json.dumps-based) token cost of msgs[old_index:as_of_index]
        # — captured here and carried into folded_tokens below so
        # token_cutter never has to re-derive it by re-scanning this range
        # (which will only ever grow, never shrink or change content once
        # folded — see store_msgs's append-only contract).
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
            # Summarization failed — don't advance the checkpoint. Advancing
            # anyway was the original bug: it would mark this range as
            # folded when it never actually got summarized, so it drops out
            # of context for good. Leaving as_of_index alone means the next
            # over-threshold turn just retries the same range.
            return
        store_checkpoint(project_dir, new_summary, as_of_index, folded_tokens)
        # Own task, not awaited here — see _review_memory's docstring for
        # why: awaiting it inline would hold project_dir in _compacting for
        # as long as the review takes, and a stuck review would then wedge
        # every future compaction for this project until process restart.
        asyncio.create_task(_review_memory(new_summary, model, project_dir))
    finally:
        _compacting.discard(project_dir)


_SEARCH_SNIPPET_RADIUS = 160  # chars of context kept on each side of a match


def _flatten_content_for_search(content) -> str:
    """Join every text-bearing piece of a message's content into one
    searchable string — thinking, text, tool_use input, tool_result content
    (recursing into its own content list) — skipping raw image bytes.
    Mirrors _strip_images_for_summary's shape-walk below but flattens to
    text instead of replacing images with a placeholder block. A tool
    result that was persisted to disk (see tool_result_storage.py — a large
    output gets swapped for a short preview) is only searchable via that
    preview text, same as everything else that reads msgs post-persist."""
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
    """A window of `text` centered on `query`'s first case-insensitive hit
    — same idea as a grep -C context line, sized by _SEARCH_SNIPPET_RADIUS."""
    low = text.lower()
    i = low.find(query.lower())
    if i == -1:
        return text[: _SEARCH_SNIPPET_RADIUS * 2]
    start = max(0, i - _SEARCH_SNIPPET_RADIUS)
    end = min(len(text), i + len(query) + _SEARCH_SNIPPET_RADIUS)
    return ("…" if start > 0 else "") + text[start:end] + ("…" if end < len(text) else "")


def search_msgs(project_dir: str, query: str, limit: int = 10) -> list[dict]:
    """Full-text (case-insensitive substring) search over EVERY message
    ever persisted for this project — local messages.jsonl, or Postgres
    (see _use_postgres) — including anything already folded out of the live
    context by compact_checkpoint. This is the "forever lookback" half of
    compaction: folded messages are never deleted (store_msgs only ever
    appends), just not replayed into the live prompt — this is how the
    model reaches back into them on demand. See search_history_tool_.py,
    the model-facing tool this backs, and token_cutter's recovery_note,
    which points the model at it right when a checkpoint fold happens.

    Deliberately plain substring matching, not a real FTS/vector index —
    matches this module's existing complexity level (a linear jsonl scan,
    same as every other local-backend read here) and keeps local/Postgres
    behavior identical rather than one being "smarter" than the other.

    Returns [{"index": int, "role": str, "snippet": str}, ...], most recent
    match first. `index` is a jsonl line number locally, or a Postgres row
    id (a global sequence, not per-project) — display-only either way, not
    something a caller cross-references against a live msgs list.
    """
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
    """Replace base64 image data in a message's content with a short text
    placeholder before it's replayed into the summarization call (see
    _call_summary_model below) — everything else (text, tool_use shape,
    tool_result wrapping) passes through untouched.

    A single screenshot easily runs ~100k tokens of raw base64 (observed:
    394,604 base64 chars from one read_ of a screenshot — see the
    tool_result block a read_ call produces, file_tools_.py). Replaying
    that verbatim as literal message content to the summarizer is pure
    token waste (it can't render pixels) AND visibly derails it: observed
    in production (with Haiku doing the summarizing) it broke out of
    summarizing to narrate "I don't have access to bash tools in this
    environment" — the huge, structurally-real-looking tool_use/
    tool_result/image turn apparently reads as "you're mid-agentic-turn,
    respond as the agent" rather than "here is conversation history to
    summarize."""
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
    """Delete the checkpoint — the exact same summary.json/pg row
    erase_summary already wipes (see load_checkpoint/store_checkpoint: it's
    one slot, repurposed, not a second store). Exposed under its own name
    purely so a /clear call site can say "erase the summary AND the
    checkpoint" and have both actually be true in the code, rather than
    relying on a reader already knowing the two share storage."""
    erase_summary(project_dir)


_MEMORY_REVIEW_MAX_ROUNDS = 8


def _store_memory_review_recap(project_dir: str, recap: str) -> None:
    """Tiny local marker file the live TUI polls to flash a status line once
    _review_memory finishes making a real change — same idea as the
    checkpoint's as_of_index (start_live_tui_._update_status_bar polls that
    the same way), just its own file since a recap isn't part of any
    surface's durable conversation state, only a transient "something just
    happened" signal. Local-only, not Postgres-routed like messages/
    checkpoint — this is a TUI presentation concern, nothing else reads it
    back. Only called when change_log is non-empty (see _review_memory) —
    a no-op review never touches this file, so the TUI never flashes for
    "reviewed and found nothing worth changing"."""
    storage_dir = _get_storage_dir(project_dir)
    (storage_dir / "memory_review.json").write_text(json.dumps({
        "recap": recap,
        "ts": datetime.datetime.now().isoformat(),
    }))


def load_memory_review_recap(project_dir: str) -> dict | None:
    """Read back what _store_memory_review_recap last wrote —
    {"recap": str, "ts": iso str} — or None if no review has ever produced
    a change for this project. See start_live_tui_'s memory-review poll,
    the only real reader."""
    storage_dir = _get_storage_dir(project_dir)
    path = storage_dir / "memory_review.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return None


async def _review_memory(new_summary: str, model: str, project_dir: str) -> None:
    """Fired once per real compaction fold — see compact_checkpoint, called
    right after store_checkpoint succeeds. Replaces the old DEEP_MEMORY_PROMPT
    turn (claude_loop_'s deep_memory param, removed), which only ever ran
    once at the very end of a whole run; this runs silently in the same
    background task compaction already uses, every time there's fresh
    material worth checking against memory — not just once at the end.

    Lists BOTH scope manifests up front so the model can genuinely choose
    add vs. edit vs. delete instead of only ever adding — shown no existing
    keys, it has no way to know something it's about to add is actually a
    near-duplicate or a contradiction of an entry that's already there.

    Bounded to _MEMORY_REVIEW_MAX_ROUNDS tool-call rounds — each round can
    carry several memory_ calls at once (every tool_use block in a single
    response gets executed, not just the first), so a review that needs to
    inspect several entries before deciding isn't actually round-starved:
    "get key A, get key B" is one round, not two. The cap exists so a stuck
    model can't loop forever in an unattended background task; a real
    review is usually 0-2 rounds (nothing to change, or one add/edit).

    Deliberately NOT awaited inline by compact_checkpoint — see the
    asyncio.create_task call site. compact_checkpoint holds project_dir in
    _compacting for the duration of whatever it awaits directly; if this
    function hung (a slow retry loop, a model stuck near the round cap)
    while awaited inline, it would block every future compaction for that
    project_dir until process restart. Decoupled as its own task instead,
    so a stuck review only ever wastes its own task — headless's
    _drain_background_tasks still sweeps it up via asyncio.all_tasks().

    Swallows every exception — a broken memory review must never surface as
    a compaction failure; store_checkpoint has already succeeded by the
    time this runs, so there's nothing left here worth failing loudly over.
    """
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

        # "+ key" / "~ key" / "- key" per successful add/edit/delete — get/
        # list calls (inspection, not a change) never land here. Built up
        # across every round so a change made early still gets flashed even
        # if a later round errors or the round cap is hit — see the
        # unconditional check right after the loop, not inside it.
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
            # Each round is its own billed API call (up to
            # _MEMORY_REVIEW_MAX_ROUNDS of them) — never tracked before,
            # so this whole feature's real spend was invisible. total_input
            # (not "input" — that field is the live conversation's own
            # context snapshot; a background review call has nothing to do
            # with it) plus output, same running-total fields claude_loop_
            # feeds from the main turn loop.
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
    """Call the same model driving the main conversation to produce/enrich a
    summary — not a fixed smaller model, so there's one token budget to
    reason about (see compact_checkpoint) instead of the main model's and a
    separate summarizer's. Replays the real bounded range being folded
    (`messages`, already boundary-clean — see compact_checkpoint) as actual
    message objects rather than a hand-formatted "{role}: {content}"
    string. Lets this call's transcript prefix match what the main
    conversation already sent the provider, hitting its warm prompt cache —
    the same trick DSH's compaction-basic uses — instead of paying full
    price on every summarization call.

    `messages` always starts on role "user" per compact_checkpoint's
    boundary invariant (old_index/as_of_index only ever land right after a
    completed tool_result turn or at the very first user query) — no
    alternation fixup needed on that end. Ends on whatever role it ends on.

    A trailing reminder gets appended right before generation (see below) —
    a system-prompt instruction stated once, before a long real transcript,
    isn't enough on its own: observed in production, a 168-message fold
    came back as one sentence that just continued the last assistant
    message's train of thought instead of summarizing anything. The
    transcript shape (real tool_use/tool_result turns) is a much stronger
    pull toward "keep going as the agent" than a system message from
    hundreds of turns back is toward "stop and summarize." Anchoring the
    instruction as the very last thing before generation fixes that.

    `summary_cap`: see SUMMARY_CAP_FRACTION above for why this isn't a flat
    2000 tokens.

    Returns None on failure (API error, timeout, empty response) — never
    falls back to prev_summary here, so the caller can tell "summarized"
    apart from "failed" and knows not to advance the checkpoint.
    """
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
    # Anchor the instruction right before generation — see docstring. If the
    # transcript ends on role "user" (e.g. a tool_result), append as a new
    # message would violate the API's strict role alternation, so fold the
    # trailer into that last message instead.
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

        # Summarization is a non-stream call — never goes through
        # claude_loop_'s api_usage accumulation, so without this its real
        # spend (this call resends a whole slice of transcript as input,
        # to summarize it) silently never hit token_stats/tokens.json.
        # total_input, not "input" — see _review_memory's identical note.
        usage = resp.usage if resp else {}
        in_tok, out_tok = usage.get("input", 0), usage.get("output", 0)
        if in_tok or out_tok:
            token_stats["total_input"] += in_tok
            token_stats["output"] += out_tok
            save_token_stats(project_dir)

        if resp and resp.content:
            # resp.content[0] isn't reliably the text block — extended
            # thinking (already a live feature here, see
            # etype_handler_tui_.handle_thinking_delta) puts a
            # BetaThinkingBlock first when the model thinks before
            # answering, and it has .thinking, not .text. Find the actual
            # text block by type instead of assuming position.
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
