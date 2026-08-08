import sqlite3
from pathlib import Path
from contextlib import contextmanager
# ===========================================
DB_PATH = Path(__file__).resolve().parent.parent / "atl_bot.db"

# --- status values ---------------------------------------------------
STATUS_PENDING_SUMMARY = "pending_summary"
STATUS_PENDING_REVIEW  = "pending_review"
STATUS_DECLINED        = "declined"
STATUS_AWAITING_DM     = "awaiting_dm"
STATUS_KYC_IN_PROGRESS = "kyc_in_progress"
STATUS_PENDING_ACCESS  = "pending_access"
STATUS_ACTIVE          = "active"
STATUS_REMOVED         = "removed"

ALL_STATUSES = (
    STATUS_PENDING_SUMMARY, STATUS_PENDING_REVIEW, STATUS_DECLINED,
    STATUS_AWAITING_DM, STATUS_KYC_IN_PROGRESS, STATUS_PENDING_ACCESS,
    STATUS_ACTIVE, STATUS_REMOVED,
)

_STATUS_SQL_LIST = ", ".join(f"'{s}'" for s in ALL_STATUSES)

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS members (
    user_id         INTEGER PRIMARY KEY,
    username        TEXT,
    first_name      TEXT,
    last_name       TEXT,
    status          TEXT NOT NULL DEFAULT '{STATUS_PENDING_SUMMARY}'
                    CHECK (status IN ({_STATUS_SQL_LIST})),
    kyc_field_index INTEGER NOT NULL DEFAULT 0,
    first_seen      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_members_status   ON members(status);
CREATE INDEX IF NOT EXISTS idx_members_username ON members(username);

CREATE TABLE IF NOT EXISTS applications (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id           INTEGER NOT NULL REFERENCES members(user_id),
    summary_text      TEXT,
    source_chat_id    INTEGER,
    source_message_id INTEGER,
    decision          TEXT CHECK (decision IN ('approved', 'declined')),
    decided_by        INTEGER,
    decided_at        TEXT,
    created_at        TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_applications_user ON applications(user_id);

CREATE TABLE IF NOT EXISTS member_events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       INTEGER NOT NULL REFERENCES members(user_id),
    event         TEXT NOT NULL,
    actor_user_id INTEGER,
    note          TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_events_user ON member_events(user_id);

CREATE TABLE IF NOT EXISTS kyc_responses (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL REFERENCES members(user_id),
    field_key  TEXT NOT NULL,
    value_text TEXT,
    file_ref   TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(user_id, field_key)
);
"""


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    with get_conn() as conn:
        conn.executescript(SCHEMA)


# --- members ---------------------------------------------------------
def get_member(user_id):
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM members WHERE user_id = ?", (user_id,)
        ).fetchone()


def upsert_member(user_id, username=None, first_name=None, last_name=None):
    """Create the member if new; refresh their Telegram details if not.
    Never touches status."""
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO members (user_id, username, first_name, last_name)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username   = excluded.username,
                first_name = excluded.first_name,
                last_name  = excluded.last_name,
                updated_at = datetime('now')
            """,
            (user_id, username, first_name, last_name),
        )


def set_status(user_id, status, actor_user_id=None, note=None):
    """Change status and write the audit event in ONE transaction."""
    if status not in ALL_STATUSES:
        raise ValueError(f"Unknown status: {status}")
    with get_conn() as conn:
        conn.execute(
            "UPDATE members SET status = ?, updated_at = datetime('now') "
            "WHERE user_id = ?",
            (status, user_id),
        )
        conn.execute(
            "INSERT INTO member_events (user_id, event, actor_user_id, note) "
            "VALUES (?, ?, ?, ?)",
            (user_id, f"status:{status}", actor_user_id, note),
        )


# --- kyc -------------------------------------------------------------
def save_kyc_answer(user_id, field_key, value_text=None, file_ref=None):
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO kyc_responses (user_id, field_key, value_text, file_ref)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id, field_key) DO UPDATE SET
                value_text = excluded.value_text,
                file_ref   = excluded.file_ref,
                created_at = datetime('now')
            """,
            (user_id, field_key, value_text, file_ref),
        )


def get_kyc_answers(user_id):
    """Return {field_key: row} for one member."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM kyc_responses WHERE user_id = ?", (user_id,)
        ).fetchall()
    return {r["field_key"]: r for r in rows}


def set_kyc_index(user_id, index):
    with get_conn() as conn:
        conn.execute(
            "UPDATE members SET kyc_field_index = ?, "
            "updated_at = datetime('now') WHERE user_id = ?",
            (index, user_id),
        )
