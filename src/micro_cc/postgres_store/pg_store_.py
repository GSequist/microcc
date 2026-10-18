import datetime
import json
import os

from micro_cc.postgres_store import schema_
from micro_cc.utils.helpers import project_hash
from micro_cc.utils.msg_normalize_ import normalize_message, reconstruct_message, repair_dangling_tool_use, is_valid_message, log_dropped_message

# One row per message (BIGSERIAL id, so insertion order is read order).
# Several processes can write one project_hash here, so writes take an advisory lock (_locked).
# store_msgs inserts msgs[COUNT(*):], so Postgres, not memory, holds the write cursor.

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
    """Run fn() under a session advisory lock on phash (the cross-process analogue of msg_store_'s RLock)."""
    with conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_lock(hashtext(%s)::bigint)", (phash,))
    # Autocommit: advisory locks are session-scoped, so unlock explicitly.
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
    """Append the msgs tail not yet stored for this project. Call whenever you want state durable.

    No repair here: a mid-run tail is often a pending tool_use, and inserted rows are permanent.
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
                    # COUNT ahead of msgs means a stale view; appending would skip rows, so replace.
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
    """Repaired, reconstructed messages. Locked so it can't observe a rewind between its DELETE and re-INSERT."""
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
                    # Delete, not just filter: store_msgs' COUNT(*) must match the returned list.
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
    """ILIKE substring match over the JSONB message text, newest first; mirrors msg_store_ semantics."""
    if not query:
        return []
    # Lazy: msg_store_ imports this module at load, so a top-level import would be circular.
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
    """Checkpoint from micro_cc_sessions.summary; None if absent or old-format (no as_of_index)."""
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
    """Persist the checkpoint in the same row store_summary writes."""
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
    """Token stats dict or None; the Postgres twin of tokenization_simple's tokens.json."""
    phash = project_hash(project_dir)
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT tokens FROM micro_cc_sessions WHERE project_hash = %s",
            (phash,),
        )
        row = cur.fetchone()
    return row[0] if row and row[0] else None


def save_token_stats(project_dir: str, token_stats: dict) -> None:
    """Upsert the whole token_stats dict, as tokenization_simple writes it to tokens.json."""
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
    """Manifest [{key, description, updated_at}], newest first; project_hash '' is the global scope."""
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
    """{'status': 'ok'|'exists'}; ON CONFLICT keeps the existence check atomic across processes."""
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
    """{'status': 'ok'|'missing'}; COALESCE merges in one atomic UPDATE."""
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
    """{'status': 'ok'|'missing'}."""
    with _connect() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM micro_cc_memory WHERE key = %s AND project_hash = %s", (key, project_hash))
        status = "ok" if cur.rowcount else "missing"
    return {"status": status, "key": key}


# ---------------------------------------------------------------------------
# Settings (settings_store_'s Postgres backend — see _ensure_settings_table)
# ---------------------------------------------------------------------------

def load_settings() -> dict | None:
    """None on a missing row, so settings_store_ can tell unwritten from {}."""
    with _connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT data FROM micro_cc_settings WHERE id = 'global'")
        row = cur.fetchone()
    return row[0] if row else None


def load_theme() -> dict | None:
    """Theme row of micro_cc_settings; None if never written. Falls back to the legacy micro_cc_theme table."""
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
        # Idempotent: racing processes write the same row; legacy table left in place.
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
    """Whole-dict upsert; settings are human-edited, so no append/lock path like store_msgs."""
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
