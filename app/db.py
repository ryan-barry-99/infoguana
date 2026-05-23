import json
import re
import sqlite3
import struct
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

import sqlite_vec

from app.config import settings
from app.models import Attachment, Chat, Edge, EdgeType, EdgeView, Message, MessageAttachment, Note, NoteCreate, NoteType, NoteUpdate


EMBED_DIM = 384  # bge-small-en-v1.5
RRF_K = 60       # reciprocal rank fusion constant


def _vec_blob(vec: list[float]) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec)


SCHEMA = """
CREATE TABLE IF NOT EXISTS notes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    content     TEXT NOT NULL,
    description TEXT,
    preview     TEXT,                         -- haiku-generated short summary; first-line truncation as fallback
    type        TEXT NOT NULL DEFAULT 'unsorted',
    project     TEXT,
    tags        TEXT NOT NULL DEFAULT '[]',  -- JSON array
    source      TEXT NOT NULL DEFAULT 'web',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    status      TEXT,                         -- only meaningful for type IN ('plan','task'): not_started|pending|complete
    linked_prs  TEXT NOT NULL DEFAULT '[]',   -- JSON array of PR URLs, for completed plans
    due_date    TEXT,                         -- only meaningful for plans/tasks: ISO 'YYYY-MM-DD' in the user's local TZ
    version     INTEGER NOT NULL DEFAULT 1,  -- bumped on every update_note; matches the live row's version number (plan #166)
    -- Plan #167. Trust dimension on the captured claim. 'stated' = user told
    -- me explicitly; 'inferred' = I derived it from a diff/PR/code; 'speculative'
    -- = I extrapolated; 'unspecified' = pre-#167 or skipped. Never auto-promoted.
    confidence       TEXT NOT NULL DEFAULT 'unspecified',
    -- Free-text source detail ("user statement 2026-04-21 chat", "inferred from
    -- PR #42 review", "web: example.com/foo"). Optional — confidence carries
    -- the queryable signal; this captures nuance the enum can't.
    provenance_note  TEXT
);

-- Per-note revision log. A row is captured before each destructive change
-- (update or delete), so the live `notes` row always represents version
-- N and `note_versions` holds versions 1..N-1 (plus a final row with
-- change_kind='delete' if the note was hard-deleted). No FK to notes —
-- versions outlive deletion so the audit trail survives. Plan #166.
CREATE TABLE IF NOT EXISTS note_versions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    note_id     INTEGER NOT NULL,
    version     INTEGER NOT NULL,
    content     TEXT NOT NULL,
    description TEXT,
    preview     TEXT,
    type        TEXT NOT NULL,
    project     TEXT,
    tags        TEXT NOT NULL DEFAULT '[]',
    status      TEXT,
    linked_prs  TEXT NOT NULL DEFAULT '[]',
    due_date    TEXT,
    edited_at   TEXT NOT NULL,
    change_kind TEXT NOT NULL,                -- 'update' | 'delete'
    confidence       TEXT NOT NULL DEFAULT 'unspecified',  -- plan #167
    provenance_note  TEXT,                                 -- plan #167
    UNIQUE(note_id, version)
);
CREATE INDEX IF NOT EXISTS idx_note_versions_note ON note_versions(note_id, version DESC);

CREATE INDEX IF NOT EXISTS idx_notes_project ON notes(project);
CREATE INDEX IF NOT EXISTS idx_notes_type    ON notes(type);
CREATE INDEX IF NOT EXISTS idx_notes_created ON notes(created_at DESC);

CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts USING fts5(
    content,
    description,
    tags,
    content='notes',
    content_rowid='id',
    tokenize='porter unicode61'
);

CREATE TRIGGER IF NOT EXISTS notes_fts_ai AFTER INSERT ON notes BEGIN
    INSERT INTO notes_fts(rowid, content, description, tags)
    VALUES (new.id, new.content, COALESCE(new.description, ''), new.tags);
END;
CREATE TRIGGER IF NOT EXISTS notes_fts_ad AFTER DELETE ON notes BEGIN
    INSERT INTO notes_fts(notes_fts, rowid, content, description, tags)
    VALUES('delete', old.id, old.content, COALESCE(old.description, ''), old.tags);
END;
CREATE TRIGGER IF NOT EXISTS notes_fts_au AFTER UPDATE ON notes BEGIN
    INSERT INTO notes_fts(notes_fts, rowid, content, description, tags)
    VALUES('delete', old.id, old.content, COALESCE(old.description, ''), old.tags);
    INSERT INTO notes_fts(rowid, content, description, tags)
    VALUES (new.id, new.content, COALESCE(new.description, ''), new.tags);
END;

CREATE TABLE IF NOT EXISTS attachments (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    note_id     INTEGER NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
    path        TEXT NOT NULL,  -- relative to attachments_dir
    mime_type   TEXT,
    size_bytes  INTEGER,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_attachments_note ON attachments(note_id);

CREATE TABLE IF NOT EXISTS links (
    from_id   INTEGER NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
    to_id     INTEGER NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
    relation  TEXT NOT NULL DEFAULT 'related',
    created_at TEXT NOT NULL,
    PRIMARY KEY (from_id, to_id, relation)
);

CREATE TABLE IF NOT EXISTS edges (
    from_id            INTEGER NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
    to_id              INTEGER NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
    edge_type          TEXT NOT NULL,
    created_by_agent   INTEGER NOT NULL DEFAULT 0,
    confirmed_by_user  INTEGER NOT NULL DEFAULT 0,
    created_at         TEXT NOT NULL,
    PRIMARY KEY (from_id, to_id, edge_type)
);
CREATE INDEX IF NOT EXISTS idx_edges_to ON edges(to_id);

CREATE TABLE IF NOT EXISTS projects (
    name         TEXT PRIMARY KEY,
    path         TEXT,
    description  TEXT,
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS chats (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    title       TEXT NOT NULL DEFAULT 'new chat',
    model       TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chats_updated ON chats(updated_at DESC);

CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id     INTEGER NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
    role        TEXT NOT NULL,            -- user | assistant | system
    content     TEXT NOT NULL,
    tool_calls  TEXT,                     -- JSON array of {name, args, result}
    created_at  TEXT NOT NULL,
    run_status  TEXT                      -- assistant only: running|complete|error|interrupted; NULL = legacy/complete
);
CREATE INDEX IF NOT EXISTS idx_messages_chat ON messages(chat_id, id);

-- Per-message event log so the chat agent can keep running after the
-- browser disconnects, and so a re-attaching tab can replay everything
-- it missed.
CREATE TABLE IF NOT EXISTS message_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id  INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    seq         INTEGER NOT NULL,         -- per-message monotonic, starts at 0
    event_type  TEXT NOT NULL,            -- text|tool_use_start|tool_use|tool_result|final|error|done
    payload     TEXT NOT NULL,            -- JSON
    created_at  TEXT NOT NULL,
    UNIQUE(message_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_message_events_lookup ON message_events(message_id, seq);

CREATE TABLE IF NOT EXISTS message_attachments (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id    INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    path          TEXT NOT NULL,  -- relative to attachments_dir
    mime_type     TEXT,
    size_bytes    INTEGER,
    original_name TEXT,
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_message_attachments_message ON message_attachments(message_id);

CREATE TABLE IF NOT EXISTS protocol (
    key         TEXT PRIMARY KEY,
    content     TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS fs_reads (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    chat_id     INTEGER REFERENCES chats(id) ON DELETE SET NULL,
    tool        TEXT NOT NULL,  -- 'read_file' | 'list_dir' | 'grep'
    path        TEXT NOT NULL,
    bytes       INTEGER NOT NULL DEFAULT 0,
    result      TEXT NOT NULL   -- 'ok' | 'truncated' | 'ok:N' | 'denied' | 'error:...'
);
CREATE INDEX IF NOT EXISTS idx_fs_reads_ts ON fs_reads(ts DESC);
"""


VEC_SCHEMA = f"""
CREATE VIRTUAL TABLE IF NOT EXISTS notes_vec USING vec0(
    note_id  INTEGER PRIMARY KEY,
    embedding FLOAT[{EMBED_DIM}]
);
"""


def _connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _ensure_column(conn: sqlite3.Connection, table: str, col: str, decl: str) -> None:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    if not any(r["name"] == col for r in rows):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")


_conn: Optional[sqlite3.Connection] = None


def init_db() -> sqlite3.Connection:
    global _conn
    _conn = _connect(settings.db_path)
    _conn.executescript(SCHEMA)
    _conn.executescript(VEC_SCHEMA)
    # Lightweight migrations for older dbs.
    _ensure_column(_conn, "notes", "description", "TEXT")
    _ensure_column(_conn, "notes", "preview", "TEXT")
    _ensure_column(_conn, "notes", "status", "TEXT")
    _ensure_column(_conn, "notes", "linked_prs", "TEXT NOT NULL DEFAULT '[]'")
    _ensure_column(_conn, "notes", "due_date", "TEXT")
    _ensure_column(_conn, "notes", "version", "INTEGER NOT NULL DEFAULT 1")
    _ensure_column(_conn, "notes", "confidence", "TEXT NOT NULL DEFAULT 'unspecified'")
    _ensure_column(_conn, "notes", "provenance_note", "TEXT")
    _ensure_column(_conn, "note_versions", "confidence", "TEXT NOT NULL DEFAULT 'unspecified'")
    _ensure_column(_conn, "note_versions", "provenance_note", "TEXT")
    _ensure_column(_conn, "chats", "project", "TEXT")
    _ensure_column(_conn, "messages", "run_status", "TEXT")
    _ensure_column(_conn, "projects", "hidden", "INTEGER NOT NULL DEFAULT 0")
    # Plan #279: NoteType 'project' was renamed to 'feature' to disambiguate
    # from the per-note `project` string field. Idempotent rekey on startup.
    _conn.execute("UPDATE notes SET type = 'feature' WHERE type = 'project'")
    # Index that depends on columns the migration may have just added.
    # Plan #289: 'task' joins 'plan' as a tracked-work type with the same
    # lifecycle. Drop the old plan-only partial index so we can rebuild it
    # over both types — partial-index predicates aren't editable in place.
    _conn.execute("DROP INDEX IF EXISTS idx_notes_plan_status")
    _conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_notes_plan_status "
        "ON notes(project, status) WHERE type IN ('plan', 'task')"
    )
    _conn.commit()
    # First-boot seeding of universal global rules. Idempotent — gated by
    # an `app_meta` sentinel so deleted rules stay deleted across restarts.
    from app import seed_rules
    seed_rules.seed_if_needed(_conn)
    return _conn


def get_conn() -> sqlite3.Connection:
    if _conn is None:
        raise RuntimeError("DB not initialized")
    return _conn


@contextmanager
def tx() -> Iterator[sqlite3.Connection]:
    conn = get_conn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def _attachments_for(conn: sqlite3.Connection, note_id: int) -> list[Attachment]:
    rows = conn.execute(
        "SELECT * FROM attachments WHERE note_id = ? ORDER BY id", (note_id,)
    ).fetchall()
    return [
        Attachment(
            id=r["id"],
            note_id=r["note_id"],
            path=r["path"],
            mime_type=r["mime_type"],
            size_bytes=r["size_bytes"],
            created_at=datetime.fromisoformat(r["created_at"]),
        )
        for r in rows
    ]


def _row_to_note(row: sqlite3.Row, conn: Optional[sqlite3.Connection] = None) -> Note:
    c = conn or get_conn()
    keys = row.keys()
    return Note(
        id=row["id"],
        content=row["content"],
        description=row["description"] if "description" in keys else None,
        preview=row["preview"] if "preview" in keys else None,
        type=row["type"],
        project=row["project"],
        tags=json.loads(row["tags"]),
        source=row["source"],
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
        status=row["status"] if "status" in keys else None,
        linked_prs=json.loads(row["linked_prs"]) if ("linked_prs" in keys and row["linked_prs"]) else [],
        due_date=row["due_date"] if "due_date" in keys else None,
        version=row["version"] if "version" in keys and row["version"] is not None else 1,
        confidence=(row["confidence"] if "confidence" in keys and row["confidence"] else "unspecified"),
        provenance_note=row["provenance_note"] if "provenance_note" in keys else None,
        attachments=_attachments_for(c, row["id"]),
    )


def create_note(data: NoteCreate) -> Note:
    now = datetime.now(timezone.utc).isoformat()
    note_type = data.type or "unsorted"
    # Plans and tasks default to not_started if the caller didn't say
    # otherwise; for other types status stays NULL. Existing pending notes
    # remain pending — the default only governs newly-created tracked-work
    # notes that didn't specify a state.
    status = data.status
    if note_type in ("plan", "task") and status is None:
        status = "not_started"
    elif note_type not in ("plan", "task"):
        status = None
    due_date = data.due_date if note_type in ("plan", "task") and data.due_date else None
    confidence = data.confidence or "unspecified"
    provenance_note = (data.provenance_note or None) if data.provenance_note != "" else None
    with tx() as conn:
        cur = conn.execute(
            """
            INSERT INTO notes (content, type, project, tags, source, created_at,
                               updated_at, status, linked_prs, due_date,
                               confidence, provenance_note)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                data.content,
                note_type,
                data.project,
                json.dumps(data.tags),
                data.source,
                now,
                now,
                status,
                json.dumps(data.linked_prs),
                due_date,
                confidence,
                provenance_note,
            ),
        )
        row = conn.execute("SELECT * FROM notes WHERE id = ?", (cur.lastrowid,)).fetchone()
        note = _row_to_note(row, conn)
    return note


def get_note(note_id: int) -> Optional[Note]:
    conn = get_conn()
    row = conn.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone()
    return _row_to_note(row, conn) if row else None


def _snapshot_version(conn: sqlite3.Connection, note_id: int, change_kind: str) -> None:
    """Snapshot the current notes row into note_versions before a destructive
    change. The snapshot's `version` matches the row's current version
    column — the *live* state being archived. Caller bumps notes.version
    afterward (for updates) or deletes the row (for deletes)."""
    row = conn.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone()
    if not row:
        return
    keys = row.keys()
    conn.execute(
        """
        INSERT INTO note_versions (note_id, version, content, description, preview,
                                   type, project, tags, status, linked_prs, due_date,
                                   edited_at, change_kind, confidence, provenance_note)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            note_id,
            row["version"] if "version" in keys and row["version"] is not None else 1,
            row["content"],
            row["description"] if "description" in keys else None,
            row["preview"] if "preview" in keys else None,
            row["type"],
            row["project"],
            row["tags"],
            row["status"] if "status" in keys else None,
            row["linked_prs"] if "linked_prs" in keys else "[]",
            row["due_date"] if "due_date" in keys else None,
            datetime.now(timezone.utc).isoformat(),
            change_kind,
            (row["confidence"] if "confidence" in keys and row["confidence"] else "unspecified"),
            row["provenance_note"] if "provenance_note" in keys else None,
        ),
    )


def get_tombstone(note_id: int) -> Optional[Note]:
    """Return the final-state tombstone snapshot for a deleted note as a
    synthetic Note (with `tombstoned=True`), or None if the note isn't
    deleted (or never existed). Used by /browse to surface deleted notes
    in search/jump results."""
    conn = get_conn()
    row = conn.execute(
        """
        SELECT * FROM note_versions
        WHERE note_id = ? AND change_kind = 'delete'
        ORDER BY version DESC LIMIT 1
        """,
        (note_id,),
    ).fetchone()
    if not row:
        return None
    return _tombstone_row_to_note(row)


def search_tombstones(query: str, limit: int = 20) -> list[Note]:
    """LIKE-based search across deleted notes. Tombstone rows aren't in
    FTS or sqlite-vec, so we can't reuse hybrid_search — but the corpus
    of deleted notes is tiny relative to live notes, and a substring match
    is plenty for the "I deleted something, find it" use case."""
    if not query.strip():
        return []
    pat = f"%{query.strip()}%"
    conn = get_conn()
    rows = conn.execute(
        """
        SELECT * FROM note_versions
        WHERE change_kind = 'delete'
          AND (content LIKE ? OR tags LIKE ?)
        ORDER BY edited_at DESC
        LIMIT ?
        """,
        (pat, pat, limit),
    ).fetchall()
    return [_tombstone_row_to_note(r) for r in rows]


def _tombstone_row_to_note(row: sqlite3.Row) -> Note:
    """Synthesize a Note from a note_versions tombstone row. The note no
    longer exists in the notes table, so we reconstruct from the snapshot:
    id and timestamps come from the version row, attachments are empty
    (deletion took those with it), `tombstoned=True` flags the rendering
    path."""
    edited = datetime.fromisoformat(row["edited_at"])
    keys = row.keys()
    return Note(
        id=row["note_id"],
        content=row["content"],
        description=row["description"],
        preview=row["preview"],
        type=row["type"],
        project=row["project"],
        tags=json.loads(row["tags"]) if row["tags"] else [],
        source="web",
        created_at=edited,  # original create lost; show deletion time as the surfaced timestamp
        updated_at=edited,
        status=row["status"],
        linked_prs=json.loads(row["linked_prs"]) if row["linked_prs"] else [],
        due_date=row["due_date"],
        version=row["version"],
        confidence=(row["confidence"] if "confidence" in keys and row["confidence"] else "unspecified"),
        provenance_note=row["provenance_note"] if "provenance_note" in keys else None,
        tombstoned=True,
    )


def list_note_versions(note_id: int) -> list[dict]:
    """Return all archived versions of a note (oldest first). Each entry is
    the snapshot taken before the change identified by change_kind."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM note_versions WHERE note_id = ? ORDER BY version ASC",
        (note_id,),
    ).fetchall()
    out: list[dict] = []
    for r in rows:
        keys = r.keys()
        out.append({
            "version": r["version"],
            "content": r["content"],
            "description": r["description"],
            "preview": r["preview"],
            "type": r["type"],
            "project": r["project"],
            "tags": json.loads(r["tags"]) if r["tags"] else [],
            "status": r["status"],
            "linked_prs": json.loads(r["linked_prs"]) if r["linked_prs"] else [],
            "due_date": r["due_date"],
            "edited_at": r["edited_at"],
            "change_kind": r["change_kind"],
            "confidence": (r["confidence"] if "confidence" in keys and r["confidence"] else "unspecified"),
            "provenance_note": r["provenance_note"] if "provenance_note" in keys else None,
        })
    return out


def update_note(note_id: int, data: NoteUpdate) -> Optional[Note]:
    fields: list[str] = []
    values: list = []
    # Track whether any user-meaningful field is actually changing so we
    # only snapshot + bump version on real edits. Preview and description
    # are system-derived (haiku classifier) and would otherwise mint a
    # bogus "edited" entry on every fresh capture.
    user_meaningful_change = False
    if data.content is not None:
        fields.append("content = ?")
        values.append(data.content)
        user_meaningful_change = True
    if data.description is not None:
        fields.append("description = ?")
        values.append(data.description)
    if data.preview is not None:
        fields.append("preview = ?")
        values.append(data.preview)
    if data.type is not None:
        fields.append("type = ?")
        values.append(data.type)
        user_meaningful_change = True
    if data.project is not None:
        fields.append("project = ?")
        values.append(data.project)
        user_meaningful_change = True
    if data.tags is not None:
        fields.append("tags = ?")
        values.append(json.dumps(data.tags))
        user_meaningful_change = True
    if data.status is not None:
        fields.append("status = ?")
        values.append(data.status)
        user_meaningful_change = True
    if data.linked_prs is not None:
        fields.append("linked_prs = ?")
        values.append(json.dumps(data.linked_prs))
        user_meaningful_change = True
    if data.due_date is not None:
        # Empty string means "clear" (None means "leave alone"); a value
        # sets it. Stored as ISO 'YYYY-MM-DD'.
        fields.append("due_date = ?")
        values.append(data.due_date or None)
        user_meaningful_change = True
    if data.confidence is not None:
        fields.append("confidence = ?")
        values.append(data.confidence)
        user_meaningful_change = True
    if data.provenance_note is not None:
        # Empty string clears the detail; any other string overwrites.
        fields.append("provenance_note = ?")
        values.append(data.provenance_note or None)
        user_meaningful_change = True
    if not fields:
        return get_note(note_id)
    fields.append("updated_at = ?")
    values.append(datetime.now(timezone.utc).isoformat())
    if user_meaningful_change:
        fields.append("version = version + 1")  # bump after snapshot captures prior state
    values.append(note_id)
    with tx() as conn:
        existing = conn.execute("SELECT 1 FROM notes WHERE id = ?", (note_id,)).fetchone()
        if not existing:
            return None
        if user_meaningful_change:
            _snapshot_version(conn, note_id, "update")
        conn.execute(f"UPDATE notes SET {', '.join(fields)} WHERE id = ?", values)
    return get_note(note_id)


def delete_note(note_id: int) -> bool:
    with tx() as conn:
        existing = conn.execute("SELECT 1 FROM notes WHERE id = ?", (note_id,)).fetchone()
        if not existing:
            return False
        _snapshot_version(conn, note_id, "delete")
        cur = conn.execute("DELETE FROM notes WHERE id = ?", (note_id,))
        conn.execute("DELETE FROM notes_vec WHERE note_id = ?", (note_id,))
    return cur.rowcount > 0


def add_attachment(note_id: int, rel_path: str, mime_type: str,
                   size_bytes: int) -> Attachment:
    now = datetime.now(timezone.utc).isoformat()
    with tx() as conn:
        cur = conn.execute(
            """
            INSERT INTO attachments (note_id, path, mime_type, size_bytes, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (note_id, rel_path, mime_type, size_bytes, now),
        )
        row = conn.execute("SELECT * FROM attachments WHERE id = ?", (cur.lastrowid,)).fetchone()
    return Attachment(
        id=row["id"],
        note_id=row["note_id"],
        path=row["path"],
        mime_type=row["mime_type"],
        size_bytes=row["size_bytes"],
        created_at=datetime.fromisoformat(row["created_at"]),
    )


def list_plans(project: Optional[str | list[str]] = None,
               status: Optional[str] = None,
               limit: int = 50, today: Optional[str] = None) -> list[Note]:
    """List tracked-work notes (plan + task), optionally filtered by project
    and/or status. Ordered:
      1. Overdue/today first (a due_date <= today, status != complete), by due_date ASC
      2. Then upcoming due dates (due_date > today, status != complete) ASC
      3. Then pending without a due date
      4. Then everything else by updated_at DESC

    `project` is either a single name or a list of names (multi-select on
    the agenda view).

    `today` is an ISO 'YYYY-MM-DD' string (caller's local TZ); defaults to
    UTC date if not passed — the caller (MCP/web) is expected to pass its
    own local date so the overdue boundary lines up with the user's clock.

    Name kept for back-compat — covers both lifecycle-bearing types since
    plan #289 added 'task' as the non-graduating sibling of 'plan'."""
    conn = get_conn()
    where = ["type IN ('plan', 'task')"]
    params: list = []
    if project:
        projects = [project] if isinstance(project, str) else list(project)
        if projects:
            placeholders = ",".join("?" * len(projects))
            where.append(f"project IN ({placeholders})")
            params.extend(projects)
    if status:
        where.append("status = ?")
        params.append(status)
    if today is None:
        today = datetime.now(timezone.utc).date().isoformat()
    # Buckets:
    #   0: not complete && due_date <= today (overdue + today)
    #   1: not complete && due_date > today (upcoming)
    #   2: not complete && due_date IS NULL && status='pending' (in-progress no date)
    #   3: everything else
    bucket = (
        "CASE "
        "WHEN status != 'complete' AND due_date IS NOT NULL AND due_date <= ? THEN 0 "
        "WHEN status != 'complete' AND due_date IS NOT NULL AND due_date > ? THEN 1 "
        "WHEN status = 'pending' AND due_date IS NULL THEN 2 "
        "ELSE 3 END"
    )
    # SQLite binds `?` left-to-right in the SQL text — WHERE placeholders
    # come first, then the two ORDER-BY-bucket placeholders, then LIMIT.
    params = [*params, today, today, limit]
    where_sql = " AND ".join(where)
    sql = f"""
        SELECT * FROM notes
        WHERE {where_sql}
        ORDER BY {bucket} ASC,
                 CASE WHEN due_date IS NULL THEN 1 ELSE 0 END ASC,
                 due_date ASC,
                 updated_at DESC
        LIMIT ?
    """
    rows = conn.execute(sql, params).fetchall()
    return [_row_to_note(r, conn) for r in rows]


def list_notes(project: Optional[str] = None,
               type: Optional[str | list[str]] = None,
               status: Optional[str] = None,
               tag: Optional[str] = None,
               limit: int = 100) -> list[Note]:
    """Filter the notes table by any combination of project / type / status / tag.
    `type` is either a single type string or a list of types (multi-select
    from /browse). `status` is only meaningful for plans/tasks but it's
    accepted unconditionally (a non-tracked-work type + status filter just
    returns nothing). `tag` matches against the tags JSON array via
    json_each — exact-match, case-sensitive. Ordered by created_at DESC for
    predictability."""
    conn = get_conn()
    where: list[str] = []
    params: list = []
    if project:
        where.append("project = ?")
        params.append(project)
    if type:
        types = [type] if isinstance(type, str) else list(type)
        if types:
            placeholders = ",".join("?" * len(types))
            where.append(f"type IN ({placeholders})")
            params.extend(types)
    if status:
        where.append("status = ?")
        params.append(status)
    if tag:
        where.append("EXISTS (SELECT 1 FROM json_each(notes.tags) WHERE value = ?)")
        params.append(tag)
    sql = "SELECT * FROM notes"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    return [_row_to_note(r, conn) for r in rows]


def recent_notes(project: Optional[str] = None, limit: int = 50) -> list[Note]:
    conn = get_conn()
    if project:
        rows = conn.execute(
            "SELECT * FROM notes WHERE project = ? ORDER BY created_at DESC LIMIT ?",
            (project, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM notes ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [_row_to_note(r, conn) for r in rows]


_FTS_BAREWORD = re.compile(r"^[A-Za-z0-9_]+$")


def _fts_sanitize(query: str) -> str:
    """Wrap each whitespace-separated term in double quotes unless it's a
    bare alphanumeric word, so FTS5 doesn't try to interpret hyphens,
    digits, or punctuation as operators / column references. Without this,
    `issue-108` is parsed as `issue MINUS 108` and 500s with "no such
    column: 108". Internal quotes are doubled per FTS5's quoting rules."""
    parts = []
    for term in query.split():
        if _FTS_BAREWORD.match(term):
            parts.append(term)
        else:
            parts.append('"' + term.replace('"', '""') + '"')
    return " ".join(parts)


def fts_search(query: str, limit: int = 20,
               type_filter: Optional[NoteType | list[NoteType]] = None,
               project_filter: Optional[str] = None) -> list[tuple[Note, float]]:
    conn = get_conn()
    where = ["notes.id = notes_fts.rowid", "notes_fts MATCH ?"]
    params: list = [_fts_sanitize(query)]
    if type_filter:
        types = [type_filter] if isinstance(type_filter, str) else list(type_filter)
        if types:
            placeholders = ",".join("?" * len(types))
            where.append(f"notes.type IN ({placeholders})")
            params.extend(types)
    if project_filter:
        where.append("notes.project = ?")
        params.append(project_filter)
    params.append(limit)
    sql = f"""
        SELECT notes.*, bm25(notes_fts) AS score
        FROM notes, notes_fts
        WHERE {' AND '.join(where)}
        ORDER BY score
        LIMIT ?
    """
    rows = conn.execute(sql, params).fetchall()
    return [(_row_to_note(r, conn), r["score"]) for r in rows]


def store_embedding(note_id: int, vec: list[float]) -> None:
    with tx() as conn:
        conn.execute("DELETE FROM notes_vec WHERE note_id = ?", (note_id,))
        conn.execute(
            "INSERT INTO notes_vec (note_id, embedding) VALUES (?, ?)",
            (note_id, _vec_blob(vec)),
        )


def vector_search(query_vec: list[float], limit: int = 20,
                  type_filter: Optional[NoteType | list[NoteType]] = None,
                  project_filter: Optional[str] = None) -> list[tuple[Note, float]]:
    """K-NN by cosine distance. Returns (note, distance) — lower is better."""
    conn = get_conn()
    over_fetch = max(limit * 3, 30) if (type_filter or project_filter) else limit
    rows = conn.execute(
        """
        SELECT notes_vec.note_id, notes_vec.distance, notes.*
        FROM notes_vec
        JOIN notes ON notes.id = notes_vec.note_id
        WHERE embedding MATCH ? AND k = ?
        ORDER BY distance
        """,
        (_vec_blob(query_vec), over_fetch),
    ).fetchall()
    types_set: Optional[set[str]] = None
    if type_filter:
        if isinstance(type_filter, str):
            types_set = {type_filter}
        else:
            types_set = set(type_filter)
    results: list[tuple[Note, float]] = []
    for r in rows:
        if types_set is not None and r["type"] not in types_set:
            continue
        if project_filter and r["project"] != project_filter:
            continue
        results.append((_row_to_note(r, conn), float(r["distance"])))
        if len(results) >= limit:
            break
    return results


def hybrid_search(query: str, query_vec: Optional[list[float]] = None, limit: int = 20,
                  type_filter: Optional[NoteType | list[NoteType]] = None,
                  project_filter: Optional[str] = None) -> list[tuple[Note, float]]:
    """Reciprocal Rank Fusion of FTS5 BM25 and vector search."""
    fts = fts_search(query, limit=limit * 2, type_filter=type_filter, project_filter=project_filter)
    vec: list[tuple[Note, float]] = []
    if query_vec is not None:
        vec = vector_search(query_vec, limit=limit * 2, type_filter=type_filter, project_filter=project_filter)

    scores: dict[int, float] = {}
    notes: dict[int, Note] = {}
    for rank, (n, _) in enumerate(fts):
        scores[n.id] = scores.get(n.id, 0.0) + 1.0 / (RRF_K + rank)
        notes[n.id] = n
    for rank, (n, _) in enumerate(vec):
        scores[n.id] = scores.get(n.id, 0.0) + 1.0 / (RRF_K + rank)
        notes[n.id] = n

    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)[:limit]
    return [(notes[nid], score) for nid, score in ranked]


def _row_to_chat(row: sqlite3.Row) -> Chat:
    return Chat(
        id=row["id"],
        title=row["title"],
        model=row["model"],
        project=row["project"] if "project" in row.keys() else None,
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )


def _message_attachments_for(conn: sqlite3.Connection,
                             message_id: int) -> list[MessageAttachment]:
    rows = conn.execute(
        "SELECT * FROM message_attachments WHERE message_id = ? ORDER BY id",
        (message_id,),
    ).fetchall()
    return [
        MessageAttachment(
            id=r["id"],
            message_id=r["message_id"],
            path=r["path"],
            mime_type=r["mime_type"],
            size_bytes=r["size_bytes"],
            original_name=r["original_name"] if "original_name" in r.keys() else None,
            created_at=datetime.fromisoformat(r["created_at"]),
        )
        for r in rows
    ]


def _row_to_message(row: sqlite3.Row,
                    conn: Optional[sqlite3.Connection] = None) -> Message:
    tc = row["tool_calls"]
    keys = row.keys()
    c = conn or get_conn()
    return Message(
        id=row["id"],
        chat_id=row["chat_id"],
        role=row["role"],
        content=row["content"],
        tool_calls=json.loads(tc) if tc else [],
        created_at=datetime.fromisoformat(row["created_at"]),
        run_status=row["run_status"] if "run_status" in keys else None,
        attachments=_message_attachments_for(c, row["id"]),
    )


def add_message_attachment(message_id: int, rel_path: str,
                           mime_type: Optional[str], size_bytes: Optional[int],
                           original_name: Optional[str]) -> MessageAttachment:
    now = datetime.now(timezone.utc).isoformat()
    with tx() as conn:
        cur = conn.execute(
            """
            INSERT INTO message_attachments
                (message_id, path, mime_type, size_bytes, original_name, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (message_id, rel_path, mime_type, size_bytes, original_name, now),
        )
        row = conn.execute(
            "SELECT * FROM message_attachments WHERE id = ?", (cur.lastrowid,),
        ).fetchone()
    return MessageAttachment(
        id=row["id"],
        message_id=row["message_id"],
        path=row["path"],
        mime_type=row["mime_type"],
        size_bytes=row["size_bytes"],
        original_name=row["original_name"],
        created_at=datetime.fromisoformat(row["created_at"]),
    )


def create_chat(model: str, title: str = "new chat",
                project: Optional[str] = None) -> Chat:
    now = datetime.now(timezone.utc).isoformat()
    with tx() as conn:
        cur = conn.execute(
            "INSERT INTO chats (title, model, project, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (title, model, project, now, now),
        )
        row = conn.execute("SELECT * FROM chats WHERE id = ?", (cur.lastrowid,)).fetchone()
    return _row_to_chat(row)


def list_chats(limit: int = 100) -> list[Chat]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM chats ORDER BY updated_at DESC LIMIT ?", (limit,)
    ).fetchall()
    return [_row_to_chat(r) for r in rows]


def get_chat(chat_id: int) -> Optional[Chat]:
    conn = get_conn()
    row = conn.execute("SELECT * FROM chats WHERE id = ?", (chat_id,)).fetchone()
    return _row_to_chat(row) if row else None


def update_chat(chat_id: int, *, title: Optional[str] = None,
                model: Optional[str] = None,
                project: Optional[str] = None,
                clear_project: bool = False,
                touch: bool = False) -> Optional[Chat]:
    fields: list[str] = []
    values: list = []
    if title is not None:
        fields.append("title = ?")
        values.append(title)
    if model is not None:
        fields.append("model = ?")
        values.append(model)
    if clear_project:
        fields.append("project = NULL")
    elif project is not None:
        fields.append("project = ?")
        values.append(project)
    if touch or fields:
        fields.append("updated_at = ?")
        values.append(datetime.now(timezone.utc).isoformat())
    if not fields:
        return get_chat(chat_id)
    values.append(chat_id)
    with tx() as conn:
        conn.execute(f"UPDATE chats SET {', '.join(fields)} WHERE id = ?", values)
    return get_chat(chat_id)


def delete_chat(chat_id: int) -> bool:
    with tx() as conn:
        cur = conn.execute("DELETE FROM chats WHERE id = ?", (chat_id,))
    return cur.rowcount > 0


def add_message(chat_id: int, role: str, content: str,
                tool_calls: Optional[list[dict]] = None,
                run_status: Optional[str] = None) -> Message:
    now = datetime.now(timezone.utc).isoformat()
    with tx() as conn:
        cur = conn.execute(
            """
            INSERT INTO messages (chat_id, role, content, tool_calls, created_at, run_status)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (chat_id, role, content,
             json.dumps(tool_calls) if tool_calls else None, now, run_status),
        )
        conn.execute("UPDATE chats SET updated_at = ? WHERE id = ?", (now, chat_id))
        row = conn.execute("SELECT * FROM messages WHERE id = ?", (cur.lastrowid,)).fetchone()
    return _row_to_message(row)


def finalize_message(message_id: int, content: str,
                     tool_calls: Optional[list[dict]],
                     run_status: str) -> None:
    """Update an in-flight assistant message with final content + status.
    Called by the chat background task when the agent run finishes."""
    with tx() as conn:
        conn.execute(
            "UPDATE messages SET content = ?, tool_calls = ?, run_status = ? "
            "WHERE id = ?",
            (content, json.dumps(tool_calls) if tool_calls else None,
             run_status, message_id),
        )


def set_message_run_status(message_id: int, run_status: str) -> None:
    with tx() as conn:
        conn.execute(
            "UPDATE messages SET run_status = ? WHERE id = ?",
            (run_status, message_id),
        )


def append_message_event(message_id: int, event_type: str,
                         payload: dict) -> int:
    """Append an event to a message's stream log. Returns the assigned seq.
    Seq is per-message monotonic — derived from MAX(seq)+1 inside the same
    transaction, so concurrent appends to the same message_id can't collide."""
    now = datetime.now(timezone.utc).isoformat()
    payload_json = json.dumps(payload)
    with tx() as conn:
        row = conn.execute(
            "SELECT COALESCE(MAX(seq), -1) + 1 AS next_seq "
            "FROM message_events WHERE message_id = ?",
            (message_id,),
        ).fetchone()
        seq = int(row["next_seq"])
        conn.execute(
            "INSERT INTO message_events "
            "(message_id, seq, event_type, payload, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (message_id, seq, event_type, payload_json, now),
        )
    return seq


def list_message_events(message_id: int, after_seq: int = -1,
                        limit: int = 1000) -> list[dict]:
    """Return events for a message with seq > after_seq, ordered by seq."""
    rows = get_conn().execute(
        "SELECT seq, event_type, payload FROM message_events "
        "WHERE message_id = ? AND seq > ? ORDER BY seq LIMIT ?",
        (message_id, after_seq, limit),
    ).fetchall()
    return [
        {"seq": r["seq"], "event_type": r["event_type"],
         "payload": json.loads(r["payload"])}
        for r in rows
    ]


def reset_running_messages() -> int:
    """Called at startup. Any assistant message still marked 'running' is
    from a previous process that died — flip to 'interrupted' and append a
    final error+done event pair so SSE clients see a clean close. Returns
    the number of messages reset."""
    rows = get_conn().execute(
        "SELECT id FROM messages WHERE run_status = 'running'"
    ).fetchall()
    count = 0
    for r in rows:
        mid = r["id"]
        try:
            append_message_event(mid, "error",
                                 {"message": "infoguana restarted mid-run"})
            append_message_event(mid, "done", {"id": mid})
            set_message_run_status(mid, "interrupted")
            count += 1
        except Exception:
            pass
    return count


def list_messages(chat_id: int) -> list[Message]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM messages WHERE chat_id = ? ORDER BY id", (chat_id,)
    ).fetchall()
    return [_row_to_message(r) for r in rows]


def get_message(message_id: int) -> Optional[Message]:
    row = get_conn().execute(
        "SELECT * FROM messages WHERE id = ?", (message_id,)
    ).fetchone()
    return _row_to_message(row) if row else None


def delete_message(message_id: int) -> bool:
    with tx() as conn:
        cur = conn.execute("DELETE FROM messages WHERE id = ?", (message_id,))
    return cur.rowcount > 0


def delete_messages_after(chat_id: int, pivot_message_id: int) -> int:
    """Delete every message in chat_id whose id > pivot_message_id. Used by
    edit-and-restart: when the user rewrites a turn, anything that came after
    is no longer relevant. Cascading FKs clean up message_events and
    message_attachments rows; attachment files on disk are left for the same
    janitor that handles delete_message leaks."""
    with tx() as conn:
        cur = conn.execute(
            "DELETE FROM messages WHERE chat_id = ? AND id > ?",
            (chat_id, pivot_message_id),
        )
    return cur.rowcount


def update_message_content(message_id: int, content: str) -> bool:
    with tx() as conn:
        cur = conn.execute(
            "UPDATE messages SET content = ? WHERE id = ?",
            (content, message_id),
        )
    return cur.rowcount > 0


def chat_has_running_message(chat_id: int) -> bool:
    row = get_conn().execute(
        "SELECT 1 FROM messages WHERE chat_id = ? AND run_status = 'running' LIMIT 1",
        (chat_id,),
    ).fetchone()
    return row is not None


def get_protocol(key: str = "default") -> Optional[str]:
    row = get_conn().execute(
        "SELECT content FROM protocol WHERE key = ?", (key,)
    ).fetchone()
    return row["content"] if row else None


def set_protocol(content: str, key: str = "default") -> None:
    now = datetime.now(timezone.utc).isoformat()
    with tx() as conn:
        conn.execute(
            """INSERT INTO protocol (key, content, updated_at) VALUES (?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET
                 content = excluded.content,
                 updated_at = excluded.updated_at""",
            (key, content, now),
        )


def seed_protocol_if_missing(default_content: str, key: str = "default") -> None:
    if get_protocol(key) is None:
        set_protocol(default_content, key)


def get_project(name: str) -> Optional[dict]:
    row = get_conn().execute(
        "SELECT name, path, description FROM projects WHERE name = ?", (name,)
    ).fetchone()
    return dict(row) if row else None


def set_project_hidden(name: str, hidden: bool) -> None:
    """Hide / unhide a project from the /projects dashboard. Idempotent.
    Inserts a `projects` row if the project only existed via notes referencing
    it (no explicit row), so the hidden flag has somewhere to live."""
    now = datetime.now(timezone.utc).isoformat()
    flag = 1 if hidden else 0
    with tx() as conn:
        existing = conn.execute(
            "SELECT name FROM projects WHERE name = ?", (name,)
        ).fetchone()
        if existing:
            conn.execute("UPDATE projects SET hidden = ? WHERE name = ?", (flag, name))
        else:
            conn.execute(
                "INSERT INTO projects (name, hidden, created_at) VALUES (?, ?, ?)",
                (name, flag, now),
            )


def list_hidden_projects() -> list[str]:
    """Return names of projects flagged hidden, alphabetical."""
    rows = get_conn().execute(
        "SELECT name FROM projects WHERE hidden = 1 ORDER BY name COLLATE NOCASE"
    ).fetchall()
    return [r["name"] for r in rows]


def project_stats() -> list[dict]:
    """Per-project aggregates for the /projects dashboard. Includes projects
    with zero notes (present only in the `projects` table) so the user sees
    everything infoguana knows about, not just everything they've captured
    against. Also emits a synthetic row for notes with NULL project under
    the name '' (the template renders that as 'no project'). Sorted by
    pending-plans DESC, then last-activity DESC, so active work floats up."""
    rows = get_conn().execute(
        """
        WITH all_projects AS (
          SELECT name FROM projects WHERE hidden = 0
          UNION
          SELECT DISTINCT project FROM notes
            WHERE project IS NOT NULL AND project <> ''
            AND project NOT IN (SELECT name FROM projects WHERE hidden = 1)
        )
        SELECT
          COALESCE(p.name, '')           AS project,
          COUNT(n.id)                    AS total_notes,
          SUM(CASE WHEN n.type IN ('plan','task') AND n.status='not_started' THEN 1 ELSE 0 END) AS not_started_plans,
          SUM(CASE WHEN n.type IN ('plan','task') AND n.status='pending'     THEN 1 ELSE 0 END) AS pending_plans,
          SUM(CASE WHEN n.type IN ('plan','task') AND n.status='complete'    THEN 1 ELSE 0 END) AS complete_plans,
          MAX(n.created_at)              AS last_activity
        FROM all_projects p
        LEFT JOIN notes n ON n.project = p.name
        GROUP BY p.name

        UNION ALL

        SELECT
          ''                             AS project,
          COUNT(n.id)                    AS total_notes,
          SUM(CASE WHEN n.type IN ('plan','task') AND n.status='not_started' THEN 1 ELSE 0 END) AS not_started_plans,
          SUM(CASE WHEN n.type IN ('plan','task') AND n.status='pending'     THEN 1 ELSE 0 END) AS pending_plans,
          SUM(CASE WHEN n.type IN ('plan','task') AND n.status='complete'    THEN 1 ELSE 0 END) AS complete_plans,
          MAX(n.created_at)              AS last_activity
        FROM notes n
        WHERE (n.project IS NULL OR n.project = '')
          AND NOT EXISTS (SELECT 1 FROM projects WHERE name = '__none__' AND hidden = 1)
        HAVING COUNT(n.id) > 0

        ORDER BY pending_plans DESC, not_started_plans DESC, last_activity DESC NULLS LAST, project COLLATE NOCASE
        """
    ).fetchall()
    return [dict(r) for r in rows]


def list_project_names() -> list[str]:
    """Distinct project names known to infoguana — union of the projects
    table and any project value ever attached to a note."""
    rows = get_conn().execute(
        """
        SELECT name FROM projects
        UNION
        SELECT DISTINCT project FROM notes
          WHERE project IS NOT NULL AND project <> ''
        ORDER BY 1 COLLATE NOCASE
        """
    ).fetchall()
    return [r[0] for r in rows if r[0]]


def list_plan_project_names(include_complete: bool = False) -> list[str]:
    """Distinct project names that actually have at least one plan/task —
    used to scope the agenda's project filter so chips for projects with
    only ideas/memories/features don't clutter the picker. Without
    `include_complete`, completed plans/tasks don't keep a project in the
    chip list."""
    where = "type IN ('plan', 'task') AND project IS NOT NULL AND project <> ''"
    if not include_complete:
        where += " AND (status IS NULL OR status != 'complete')"
    rows = get_conn().execute(
        f"SELECT DISTINCT project FROM notes WHERE {where} "
        "ORDER BY project COLLATE NOCASE"
    ).fetchall()
    return [r[0] for r in rows if r[0]]


def fork_chat(source_chat_id: int, up_to_message_id: int) -> Optional[Chat]:
    """Create a new chat that copies all messages of source_chat up to and
    including up_to_message_id. New chat shares the source chat's model and
    is titled with a 'fork: ' prefix."""
    conn = get_conn()
    src = get_chat(source_chat_id)
    if not src:
        return None
    pivot = conn.execute(
        "SELECT chat_id FROM messages WHERE id = ?", (up_to_message_id,)
    ).fetchone()
    if not pivot or pivot["chat_id"] != source_chat_id:
        return None

    title = f"fork: {src.title}"[:80]
    new = create_chat(model=src.model, title=title, project=src.project)

    rows = conn.execute(
        "SELECT role, content, tool_calls, created_at FROM messages "
        "WHERE chat_id = ? AND id <= ? ORDER BY id",
        (source_chat_id, up_to_message_id),
    ).fetchall()
    with tx() as conn2:
        for r in rows:
            conn2.execute(
                """INSERT INTO messages (chat_id, role, content, tool_calls, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (new.id, r["role"], r["content"], r["tool_calls"], r["created_at"]),
            )
    return new


def _row_to_edge(row: sqlite3.Row) -> Edge:
    return Edge(
        from_id=row["from_id"],
        to_id=row["to_id"],
        edge_type=row["edge_type"],
        created_by_agent=bool(row["created_by_agent"]),
        confirmed_by_user=bool(row["confirmed_by_user"]),
        created_at=datetime.fromisoformat(row["created_at"]),
    )


def create_edge(from_id: int, to_id: int, edge_type: EdgeType,
                created_by_agent: bool = False,
                confirmed_by_user: bool = False) -> Edge:
    """Insert an edge (idempotent on the (from_id, to_id, edge_type) PK).
    Re-creating an existing edge upgrades confirmed_by_user from False -> True
    and refreshes created_at; agent-vs-user attribution stays sticky to the
    first writer."""
    now = datetime.now(timezone.utc).isoformat()
    with tx() as conn:
        conn.execute(
            """
            INSERT INTO edges (from_id, to_id, edge_type, created_by_agent,
                               confirmed_by_user, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(from_id, to_id, edge_type) DO UPDATE SET
                confirmed_by_user = MAX(confirmed_by_user, excluded.confirmed_by_user)
            """,
            (from_id, to_id, edge_type, int(created_by_agent),
             int(confirmed_by_user), now),
        )
        row = conn.execute(
            "SELECT * FROM edges WHERE from_id = ? AND to_id = ? AND edge_type = ?",
            (from_id, to_id, edge_type),
        ).fetchone()
    return _row_to_edge(row)


def delete_edge(from_id: int, to_id: int, edge_type: EdgeType) -> bool:
    with tx() as conn:
        cur = conn.execute(
            "DELETE FROM edges WHERE from_id = ? AND to_id = ? AND edge_type = ?",
            (from_id, to_id, edge_type),
        )
    return cur.rowcount > 0


def list_edges_for(note_id: int, direction: str = "both",
                   edge_types: Optional[list[str]] = None) -> list[Edge]:
    """List edges incident to a note. direction is 'out' (note as from_id),
    'in' (note as to_id), or 'both'."""
    where: list[str] = []
    params: list = []
    if direction == "out":
        where.append("from_id = ?")
        params.append(note_id)
    elif direction == "in":
        where.append("to_id = ?")
        params.append(note_id)
    else:
        where.append("(from_id = ? OR to_id = ?)")
        params.extend([note_id, note_id])
    if edge_types:
        placeholders = ",".join("?" * len(edge_types))
        where.append(f"edge_type IN ({placeholders})")
        params.extend(edge_types)
    sql = f"SELECT * FROM edges WHERE {' AND '.join(where)} ORDER BY created_at"
    rows = get_conn().execute(sql, params).fetchall()
    return [_row_to_edge(r) for r in rows]


def bundled_tasks_of(plan_id: int) -> list[Note]:
    """Return tasks bundled to a plan (incoming `bundled_with` edges from
    notes of type='task'), sorted by created_at ASC. Plan #293 convention:
    task is the from_id, plan is the to_id."""
    conn = get_conn()
    rows = conn.execute(
        """
        SELECT n.* FROM notes n
        JOIN edges e ON e.from_id = n.id
        WHERE e.to_id = ? AND e.edge_type = 'bundled_with' AND n.type = 'task'
        ORDER BY n.created_at ASC
        """,
        (plan_id,),
    ).fetchall()
    return [_row_to_note(r, conn) for r in rows]


def bundled_parent_of(task_id: int) -> Optional[Note]:
    """Return the parent plan of a task — the plan this task is bundled
    `bundled_with`. Plan #293 convention: task is the from_id, plan is the
    to_id. Returns None if no such edge exists or the target isn't a plan."""
    conn = get_conn()
    row = conn.execute(
        """
        SELECT n.* FROM notes n
        JOIN edges e ON e.to_id = n.id
        WHERE e.from_id = ? AND e.edge_type = 'bundled_with' AND n.type = 'plan'
        ORDER BY e.created_at ASC
        LIMIT 1
        """,
        (task_id,),
    ).fetchone()
    return _row_to_note(row, conn) if row else None


def traverse_edges(start_id: int, depth: int = 2, direction: str = "out",
                   edge_types: Optional[list[str]] = None) -> dict:
    """BFS from start_id along edges, depth-limited. Returns the discovered
    subgraph as {nodes: [{note_id, hops}], edges: [{from_id, to_id, edge_type}]}.

    direction: 'out' follows from_id -> to_id; 'in' follows to_id -> from_id;
    'both' walks the underlying graph as undirected.
    """
    if depth < 0:
        depth = 0
    visited: dict[int, int] = {start_id: 0}  # note_id -> hops
    frontier: list[int] = [start_id]
    out_edges: list[dict] = []
    seen_edges: set[tuple[int, int, str]] = set()

    for hop in range(depth):
        if not frontier:
            break
        next_frontier: list[int] = []
        for nid in frontier:
            for e in list_edges_for(nid, direction=direction,
                                    edge_types=edge_types):
                key = (e.from_id, e.to_id, e.edge_type)
                if key in seen_edges:
                    continue
                seen_edges.add(key)
                out_edges.append({
                    "from_id": e.from_id,
                    "to_id": e.to_id,
                    "edge_type": e.edge_type,
                })
                # Pick the neighbor on the far side of this edge relative to nid.
                neighbor = e.to_id if e.from_id == nid else e.from_id
                if neighbor not in visited:
                    visited[neighbor] = hop + 1
                    next_frontier.append(neighbor)
        frontier = next_frontier

    nodes = [{"note_id": nid, "hops": h} for nid, h in
             sorted(visited.items(), key=lambda kv: (kv[1], kv[0]))]
    return {"nodes": nodes, "edges": out_edges}


def _preview(content: str, limit: int = 80) -> str:
    """First non-empty line of `content`, trimmed and truncated. Strips
    leading markdown header hashes / list markers and trailing emphasis
    asterisks so the preview reads as plain text."""
    for raw in (content or "").splitlines():
        line = raw.strip().lstrip("#").lstrip("-*").rstrip("*").strip()
        if line:
            return line[:limit] + ("…" if len(line) > limit else "")
    return ""


def batch_links_for(note_ids: list[int],
                    confirmed_only: bool = True) -> dict[int, list[EdgeView]]:
    """For each note id in the input, return a list of EdgeView for its
    confirmed typed edges (both directions). Two queries total regardless
    of input size — one for the edges, one for the neighbor previews."""
    if not note_ids:
        return {}
    conn = get_conn()
    placeholders = ",".join("?" * len(note_ids))
    where = f"(from_id IN ({placeholders}) OR to_id IN ({placeholders}))"
    params: list = list(note_ids) + list(note_ids)
    if confirmed_only:
        where += " AND confirmed_by_user = 1"
    edge_rows = conn.execute(
        f"SELECT from_id, to_id, edge_type FROM edges WHERE {where} "
        f"ORDER BY edge_type, from_id, to_id",
        params,
    ).fetchall()
    if not edge_rows:
        return {nid: [] for nid in note_ids}

    focal = set(note_ids)
    neighbor_ids: set[int] = set()
    for r in edge_rows:
        if r["from_id"] not in focal:
            neighbor_ids.add(r["from_id"])
        if r["to_id"] not in focal:
            neighbor_ids.add(r["to_id"])
        # Self-edges and edges between two focal notes also need their
        # endpoints' previews.
        if r["from_id"] in focal:
            neighbor_ids.add(r["from_id"])
        if r["to_id"] in focal:
            neighbor_ids.add(r["to_id"])

    neighbor_meta: dict[int, dict] = {}
    if neighbor_ids:
        nph = ",".join("?" * len(neighbor_ids))
        nrows = conn.execute(
            f"SELECT id, content, type, status FROM notes WHERE id IN ({nph})",
            list(neighbor_ids),
        ).fetchall()
        for nr in nrows:
            neighbor_meta[nr["id"]] = {
                "preview": _preview(nr["content"]),
                "type": nr["type"],
                "status": nr["status"],
            }

    out: dict[int, list[EdgeView]] = {nid: [] for nid in note_ids}
    for r in edge_rows:
        for focal_id, target_id, direction in (
            (r["from_id"], r["to_id"], "out"),
            (r["to_id"], r["from_id"], "in"),
        ):
            if focal_id not in focal:
                continue
            meta = neighbor_meta.get(target_id)
            if not meta:
                continue
            out[focal_id].append(EdgeView(
                direction=direction,
                edge_type=r["edge_type"],
                target_id=target_id,
                target_preview=meta["preview"],
                target_type=meta["type"],
                target_status=meta["status"],
            ))
    return out


def attach_links(notes: list[Note]) -> None:
    """Populate `note.links` in place for every note in the list. Safe to
    call with an empty list. Used by the rendering routes that show the
    note card UI; not called by raw API endpoints."""
    if not notes:
        return
    by_id = batch_links_for([n.id for n in notes])
    for n in notes:
        n.links = by_id.get(n.id, [])
