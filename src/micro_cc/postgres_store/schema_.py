# micro_cc_sessions (parent) -> micro_cc_messages (child, FK project_hash).
# micro_cc_memory / micro_cc_settings are independent tables. micro_cc_settings holds one
# document per id: 'global' (settings) and 'theme'. micro_cc_theme is legacy, read once to migrate.

_sessions_table_ready = False
_messages_table_ready = False
_memory_table_ready = False
_settings_table_ready = False


def _ensure_sessions_table(conn) -> None:
    global _sessions_table_ready
    if _sessions_table_ready:
        return
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS micro_cc_sessions (
                project_hash TEXT PRIMARY KEY,
                project_dir TEXT NOT NULL,
                summary JSONB,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
        """)
        cur.execute(
            "ALTER TABLE micro_cc_sessions ADD COLUMN IF NOT EXISTS tokens JSONB"
        )
    _sessions_table_ready = True


def _ensure_messages_table(conn) -> None:
    # FK requires the session row to exist first — pg_store_._touch_session
    # runs before _insert_rows for exactly this reason (autocommit, no
    # shared transaction to defer the check across).
    global _messages_table_ready
    if _messages_table_ready:
        return
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS micro_cc_messages (
                id BIGSERIAL PRIMARY KEY,
                project_hash TEXT NOT NULL REFERENCES micro_cc_sessions(project_hash) ON DELETE CASCADE,
                message JSONB NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS micro_cc_messages_project_hash_id_idx
            ON micro_cc_messages (project_hash, id)
        """)
        # No "ADD CONSTRAINT IF NOT EXISTS" in Postgres — backfills the FK
        # for tables that already existed pre-migration.
        cur.execute("""
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint WHERE conname = 'micro_cc_messages_project_hash_fkey'
                ) THEN
                    ALTER TABLE micro_cc_messages
                        ADD CONSTRAINT micro_cc_messages_project_hash_fkey
                        FOREIGN KEY (project_hash) REFERENCES micro_cc_sessions(project_hash)
                        ON DELETE CASCADE;
                END IF;
            END $$;
        """)
    _messages_table_ready = True


def _ensure_memory_table(conn) -> None:
    # No FK to micro_cc_sessions: project_hash='' (global scope) has no
    # matching session row by design.
    global _memory_table_ready
    if _memory_table_ready:
        return
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS micro_cc_memory (
                project_hash TEXT NOT NULL DEFAULT '',
                key TEXT NOT NULL,
                description TEXT NOT NULL,
                content TEXT NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                PRIMARY KEY (project_hash, key)
            )
        """)
        cur.execute("ALTER TABLE micro_cc_memory ADD COLUMN IF NOT EXISTS project_hash TEXT NOT NULL DEFAULT ''")
        cur.execute("ALTER TABLE micro_cc_memory DROP CONSTRAINT IF EXISTS micro_cc_memory_pkey")
        cur.execute("ALTER TABLE micro_cc_memory ADD CONSTRAINT micro_cc_memory_pkey PRIMARY KEY (project_hash, key)")
    _memory_table_ready = True


def _ensure_settings_table(conn) -> None:
    global _settings_table_ready
    if _settings_table_ready:
        return
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS micro_cc_settings (
                id TEXT PRIMARY KEY DEFAULT 'global',
                data JSONB NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
        """)
    _settings_table_ready = True


def ensure_all(conn) -> None:
    _ensure_sessions_table(conn)
    _ensure_messages_table(conn)
    _ensure_memory_table(conn)
    _ensure_settings_table(conn)
