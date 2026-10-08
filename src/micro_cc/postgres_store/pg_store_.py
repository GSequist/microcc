import datetime
import json
import os

from micro_cc.postgres_store import schema_
from micro_cc.utils.helpers import project_hash
from micro_cc.utils.msg_normalize_ import normalize_message, reconstruct_message, repair_dangling_tool_use, is_valid_message, log_dropped_message

# One row per message (project_hash, id — id is a BIGSERIAL, so insertion
# order IS read order)
# one INSERT per message, one SELECT ... ORDER BY per read. Replaces the old
# design (_write_raw: INSERT ... ON CONFLICT DO UPDATE SET messages =
# EXCLUDED.messages), which rewrote the ENTIRE conversation into a single
# JSONB column on every single tool_call/tool_result/final_text/done/error
# event — same O(n)-per-call cost as the local backend's old full-rewrite,
# just paid as a network round trip instead of a local disk write, and with
# no way to ever become an append.
#
# store_msgs is still handed the FULL msgs list every call (that's the
# shared contract in msg_store_.py — claude_loop_ mutates one growing list
# in place, every caller just passes it along), so this still needs to know
# how much is already durable to insert only the new tail. Postgres itself
# is the durable source of truth for that count — a COUNT(*) per call,
# no in-memory cursor (which wouldn't survive a container restart anyway,
# the whole reason this backend exists).
#
# Unlike the local backend, this one CAN have two genuinely separate
# processes writing the same project_hash at once — a headless/cron run and
# a live TUI both pointed at the same Postgres and the same project_dir
# (e.g. a CRM orchestrator's per-ticket container job and an admin's local
# microcc session on that same ticket). That's not a single-writer
# deployment, so store_msgs/rewind_msgs/load_msgs all take a Postgres
# advisory lock (see _locked below) around their count-then-write critical
# sections — the local backend's in-process threading.RLock has no reach
# across a network connection, so this has to be enforced server-side
# instead.

def _connect():
    try:
        import psycopg
    except ImportError as e:
        raise RuntimeError(
            "Postgres backend selected but psycopg isn't installed — "
            "run `pip install micro-cc[postgres]`"
        ) from e
    # Embedding apps set the mirror URL for their sink without flipping the Postgres switch.
    url = os.environ.get("MICRO_CC_MIRROR_POSTGRES_URL") or os.environ["MICRO_CC_POSTGRES_URL"]
    conn = psycopg.connect(url, autocommit=True)
    schema_.ensure_all(conn)
    return conn


def _locked(conn, phash: str, fn):
    """Run fn() with a Postgres session-level advisory lock held on phash —
    the cross-process equivalent of msg_store_.py's per-project
    threading.RLock, needed because two OS processes (not just two threads
    in one process) can legitimately share a project_hash here. hashtext()
    collapses phash to a 32-bit int; a collision between two unrelated
    tickets just serializes them against each other for the duration of one
    call, never corrupts anything, so no need for a wider key.

    conn must be in autocommit mode (every caller's _connect() already is)
    — pg_advisory_lock/unlock are plain function calls, not scoped to a
    transaction, so an explicit unlock in `finally` is what actually
    releases it rather than relying on commit/rollback.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_lock(hashtext(%s)::bigint)", (phash,))
    try:
        return fn()
    finally:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_unlock(hashtext(%s)::bigint)", (phash,))


def _touch_session(cur, phash: str, project_dir: str) -> None:
    normalized = os.path.abspath(os.path.expanduser(project_dir))
    cur.execute(
        """
        INSERT INTO micro_cc_sessions (project_hash, project_dir, updated_at)
        VALUES (%s, %s, now())
        ON CONFLICT (project_hash) DO UPDATE
            SET project_dir = EXCLUDED.project_dir, updated_at = now()
        """,
        (phash, normalized),
    )


def _insert_rows(cur, phash: str, msgs: list) -> None:
    if not msgs:
        return
    cur.executemany(
        "INSERT INTO micro_cc_messages (project_hash, message) VALUES (%s, %s)",
        [(phash, json.dumps(normalize_message(m))) for m in msgs],
    )


def store_msgs(project_dir: str, msgs: list) -> None:
    """Append the tail of `msgs` not yet in micro_cc_messages for this
    project. Same contract as msg_store_.store_msgs: call it every time you
    want the latest state durable.

    Deliberately does NOT run repair_dangling_tool_use before inserting —
    same reasoning as the local backend: this is called mid-run, before a
    just-issued tool_use has its result yet, so the tail is routinely a
    dangling tool_use for a normal, brief moment. Unlike the old
    full-JSONB-overwrite design, an appended row is permanent the instant
    it's inserted — there's no next full rewrite to silently clean a
    synthetic "Interrupted" row up, so writing one here would leave it in
    history forever. Repair only belongs at load time (load_msgs/
    rewind_msgs), same as local.
    """
    phash = project_hash(project_dir)
    with _connect() as conn:
        def _do():
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) FROM micro_cc_messages WHERE project_hash = %s",
                    (phash,),
                )
                known = cur.fetchone()[0]

                if known > len(msgs):
                    # Same self-heal as msg_store_.store_msgs: a COUNT ahead
                    # of len(msgs) means this call's view of history is
                    # stale or short relative to what's actually durable —
                    # appending from `known` would skip rows outright.
                    # Replace instead of silently losing them.
                    cur.execute(
                        "DELETE FROM micro_cc_messages WHERE project_hash = %s",
                        (phash,),
                    )
                    known = 0

                # parent row before child rows (FK, autocommit)
                _touch_session(cur, phash, project_dir)
                _insert_rows(cur, phash, msgs[known:])

        _locked(conn, phash, _do)


def load_msgs(project_dir: str) -> list:
    """Locked the same as store_msgs/rewind_msgs — not because a plain read
    is unsafe by itself, but so it can't land between a concurrent rewind's
    DELETE and its re-INSERT (autocommit means those are two separate
    commits, not one transaction) and observe a transiently empty history.

    Malformed rows get DELETEd here, not just filtered in memory: unlike the
    local backend (which tracks its own high-water mark), store_msgs's
    `known` here is always a fresh COUNT(*) against this table. Merely
    filtering in memory would leave COUNT(*) ahead of the returned list by
    the dropped-row count, and store_msgs's fast path (INSERT msgs[known:])
    would then skip that many of THIS turn's genuinely new messages instead
    of just the malformed ones — same class of bug as omitting this would
    cause locally, just via COUNT(*) instead of _persisted_len.
    """
    phash = project_hash(project_dir)
    with _connect() as conn:
        def _do():
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, message FROM micro_cc_messages WHERE project_hash = %s ORDER BY id",
                    (phash,),
                )
                rows = cur.fetchall()
                bad_ids = [row_id for row_id, message in rows if not is_valid_message(message)]
                if bad_ids:
                    log_dropped_message("pg_store_", project_dir, len(bad_ids))
                    cur.execute(
                        "DELETE FROM micro_cc_messages WHERE id = ANY(%s)",
                        (bad_ids,),
                    )
                return [message for row_id, message in rows if row_id not in bad_ids]

        valid = _locked(conn, phash, _do)

    msgs = repair_dangling_tool_use(valid)
    return [reconstruct_message(m) for m in msgs]


def rewind_msgs(project_dir: str, cut_idx: int) -> list:
    phash = project_hash(project_dir)
    with _connect() as conn:
        def _do():
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT message FROM micro_cc_messages WHERE project_hash = %s ORDER BY id",
                    (phash,),
                )
                all_msgs = [row[0] for row in cur.fetchall()]

                if not 0 <= cut_idx < len(all_msgs):
                    return [reconstruct_message(m) for m in all_msgs]

                kept = repair_dangling_tool_use(all_msgs[:cut_idx])

                cur.execute(
                    "DELETE FROM micro_cc_messages WHERE project_hash = %s",
                    (phash,),
                )
                _touch_session(cur, phash, project_dir)
                _insert_rows(cur, phash, kept)

            return [reconstruct_message(m) for m in kept]

        return _locked(conn, phash, _do)


def erase_msgs(project_dir: str) -> None:
    phash = project_hash(project_dir)
    with _connect() as conn:
        def _do():
            with conn.cursor() as cur:
                cur.execute("DELETE FROM micro_cc_messages WHERE project_hash = %s", (phash,))

        _locked(conn, phash, _do)


def search_msgs(project_dir: str, query: str, limit: int = 10) -> list:
    """Postgres counterpart to msg_store_.search_msgs — ILIKE substring
    match over the JSONB message column, cast to text, rather than real
    FTS (to_tsvector/tsquery). Deliberate: mirrors the local backend's
    plain substring semantics exactly, so a project behaves identically
    regardless of which storage backend it's running against, rather than
    Postgres quietly being "smarter." Returns most-recent-first, same
    contract as the local implementation.
    """
    if not query:
        return []
    # Lazy import — msg_store_ imports pg_store_ at module load, so a
    # top-level import here would be circular.
    from micro_cc.utils.msg_store_ import _flatten_content_for_search, _snippet

    phash = project_hash(project_dir)
    pattern = f"%{query}%"
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, message FROM micro_cc_messages
            WHERE project_hash = %s AND message::text ILIKE %s
            ORDER BY id DESC LIMIT %s
            """,
            (phash, pattern, limit),
        )
        rows = cur.fetchall()

    matches = []
    for row_id, message in rows:
        text = _flatten_content_for_search(message.get("content"))
        matches.append({"index": row_id, "role": message.get("role", ""), "snippet": _snippet(text, query)})
    return matches


def load_summary(project_dir: str) -> str:
    phash = project_hash(project_dir)
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT summary FROM micro_cc_sessions WHERE project_hash = %s",
            (phash,),
        )
        row = cur.fetchone()
    if not row or not row[0]:
        return ""
    return row[0].get("content", "")


def store_summary(project_dir: str, summary: str) -> None:
    phash = project_hash(project_dir)
    normalized = os.path.abspath(os.path.expanduser(project_dir))
    payload = {"content": summary, "ts": datetime.datetime.now().isoformat()}
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO micro_cc_sessions (project_hash, project_dir, summary, updated_at)
            VALUES (%s, %s, %s, now())
            ON CONFLICT (project_hash) DO UPDATE
                SET summary = EXCLUDED.summary, updated_at = now()
            """,
            (phash, normalized, json.dumps(payload)),
        )


def load_checkpoint(project_dir: str) -> dict | None:
    """New-format checkpoint reader — same micro_cc_sessions.summary JSONB
    column load_summary reads, repurposed with an as_of_index key. Returns
    None for a missing row AND for an old-format one (no "as_of_index"),
    same contract as msg_store_.load_checkpoint. "folded_tokens" defaults
    to 0 for a row written before that field existed — see
    msg_store_.load_checkpoint's docstring."""
    phash = project_hash(project_dir)
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT summary FROM micro_cc_sessions WHERE project_hash = %s",
            (phash,),
        )
        row = cur.fetchone()
    if not row or not row[0] or "as_of_index" not in row[0]:
        return None
    return {
        "content": row[0].get("content", ""),
        "as_of_index": row[0]["as_of_index"],
        "folded_tokens": row[0].get("folded_tokens", 0),
    }


def store_checkpoint(project_dir: str, summary: str, as_of_index: int, folded_tokens: int = 0) -> None:
    """Persist the new-format checkpoint — same row store_summary writes,
    repurposed with an as_of_index and folded_tokens."""
    phash = project_hash(project_dir)
    normalized = os.path.abspath(os.path.expanduser(project_dir))
    payload = {
        "content": summary,
        "as_of_index": as_of_index,
        "folded_tokens": folded_tokens,
        "ts": datetime.datetime.now().isoformat(),
    }
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO micro_cc_sessions (project_hash, project_dir, summary, updated_at)
            VALUES (%s, %s, %s, now())
            ON CONFLICT (project_hash) DO UPDATE
                SET summary = EXCLUDED.summary, updated_at = now()
            """,
            (phash, normalized, json.dumps(payload)),
        )


def erase_summary(project_dir: str) -> None:
    phash = project_hash(project_dir)
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE micro_cc_sessions SET summary = NULL WHERE project_hash = %s",
            (phash,),
        )


def load_token_stats(project_dir: str) -> dict | None:
    """Carbon copy of the local tokens.json read — see save_token_stats.
    None on a missing row (mirrors tokenization_simple.load_token_stats'
    own no-op-on-miss local-disk contract, which the caller already relies
    on)."""
    phash = project_hash(project_dir)
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT tokens FROM micro_cc_sessions WHERE project_hash = %s",
            (phash,),
        )
        row = cur.fetchone()
    return row[0] if row and row[0] else None


def save_token_stats(project_dir: str, token_stats: dict) -> None:
    """Persist the whole token_stats dict as-is — a carbon copy of what
    tokenization_simple.save_token_stats writes to local tokens.json, not
    a bespoke column per field (this project has no per-conversation billing
    rows, and the local dict is
    already the exact shape worth keeping). Same idempotent upsert shape as
    store_summary/store_checkpoint above."""
    phash = project_hash(project_dir)
    normalized = os.path.abspath(os.path.expanduser(project_dir))
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO micro_cc_sessions (project_hash, project_dir, tokens, updated_at)
            VALUES (%s, %s, %s, now())
            ON CONFLICT (project_hash) DO UPDATE
                SET tokens = EXCLUDED.tokens, updated_at = now()
            """,
            (phash, normalized, json.dumps(token_stats)),
        )


# ---------------------------------------------------------------------------
# Long-term memory (memory_store_'s Postgres backend — see _ensure_memory_table)
# ---------------------------------------------------------------------------

def list_memories(project_hash: str = "") -> list:
    """Manifest: [{key, description, updated_at}], newest first.
    project_hash='' (default) is the global scope."""
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT key, description, updated_at FROM micro_cc_memory WHERE project_hash = %s ORDER BY updated_at DESC",
            (project_hash,),
        )
        rows = cur.fetchall()
    return [
        {"key": r[0], "description": r[1], "updated_at": r[2].isoformat() if r[2] else ""}
        for r in rows
    ]


def get_memory(key: str, project_hash: str = "") -> dict | None:
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT description, content FROM micro_cc_memory WHERE key = %s AND project_hash = %s",
            (key, project_hash),
        )
        row = cur.fetchone()
    if not row:
        return None
    return {"key": key, "description": row[0], "content": row[1]}


def count_memories(project_hash: str = "") -> int:
    with _connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM micro_cc_memory WHERE project_hash = %s", (project_hash,))
        return cur.fetchone()[0]


def add_memory(key: str, description: str, content: str, project_hash: str = "") -> dict:
    """Returns {'status': 'ok'|'exists'}. ON CONFLICT DO NOTHING makes the
    exists-check atomic — unlike the local backend, two processes sharing
    this Postgres can genuinely race on the same (project_hash, key)."""
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO micro_cc_memory (project_hash, key, description, content, updated_at)
            VALUES (%s, %s, %s, %s, now())
            ON CONFLICT (project_hash, key) DO NOTHING
            """,
            (project_hash, key, description, content),
        )
        status = "ok" if cur.rowcount else "exists"
    return {"status": status, "key": key}


def edit_memory(
    key: str,
    new_content: str | None = None,
    new_description: str | None = None,
    project_hash: str = "",
) -> dict:
    """Returns {'status': 'ok'|'missing'}. COALESCE keeps the "omit to keep
    current" merge atomic in one UPDATE, instead of a racy read-modify-write."""
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            UPDATE micro_cc_memory
            SET description = COALESCE(%s, description),
                content = COALESCE(%s, content),
                updated_at = now()
            WHERE key = %s AND project_hash = %s
            """,
            (new_description, new_content, key, project_hash),
        )
        status = "ok" if cur.rowcount else "missing"
    return {"status": status, "key": key}


def delete_memory(key: str, project_hash: str = "") -> dict:
    """Returns {'status': 'ok'|'missing'}."""
    with _connect() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM micro_cc_memory WHERE key = %s AND project_hash = %s", (key, project_hash))
        status = "ok" if cur.rowcount else "missing"
    return {"status": status, "key": key}


# ---------------------------------------------------------------------------
# Settings (settings_store_'s Postgres backend — see _ensure_settings_table)
# ---------------------------------------------------------------------------

def load_settings() -> dict | None:
    """None on a missing row — same "no row yet" contract as
    load_token_stats/load_checkpoint, so settings_store_._load() can tell
    "never written" apart from "written as {}"."""
    with _connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT data FROM micro_cc_settings WHERE id = 'global'")
        row = cur.fetchone()
    return row[0] if row else None


def load_theme() -> dict | None:
    """Theme row of micro_cc_settings; None if never written. Copies a pre-0.2.106 micro_cc_theme row once."""
    with _connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT data FROM micro_cc_settings WHERE id = 'theme'")
        row = cur.fetchone()
        if row:
            return row[0]
        cur.execute("SELECT to_regclass('micro_cc_theme') IS NOT NULL")
        if not cur.fetchone()[0]:
            return None
        cur.execute("SELECT data FROM micro_cc_theme WHERE id = 'global'")
        row = cur.fetchone()
        if not row:
            return None
        # Idempotent: racing processes upsert the same legacy data; the old table is left in place.
        cur.execute(
            "INSERT INTO micro_cc_settings (id, data, updated_at) VALUES ('theme', %s, now()) ON CONFLICT (id) DO NOTHING",
            (json.dumps(row[0]),),
        )
        return row[0]


def save_theme(store: dict) -> None:
    """Whole-dict upsert into the 'theme' row of micro_cc_settings; edited via /theme, not a hot path."""
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO micro_cc_settings (id, data, updated_at)
            VALUES ('theme', %s, now())
            ON CONFLICT (id) DO UPDATE
                SET data = EXCLUDED.data, updated_at = now()
            """,
            (json.dumps(store),),
        )


def save_settings(store: dict) -> None:
    """Whole-dict upsert — settings are edited by a human via the TUI/webui,
    not a per-message hot path, so there's no append/lock story to match
    store_msgs' here."""
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO micro_cc_settings (id, data, updated_at)
            VALUES ('global', %s, now())
            ON CONFLICT (id) DO UPDATE
                SET data = EXCLUDED.data, updated_at = now()
            """,
            (json.dumps(store),),
        )
