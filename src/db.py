"""Database layer for Alpha (ATL bot).

SQLite is the single source of truth. Excel is a generated export.
"""
import sqlite3
from pathlib import Path
from contextlib import contextmanager
# ========================================
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
    kyc_attempts    INTEGER NOT NULL DEFAULT 0,
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
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER NOT NULL REFERENCES members(user_id),
    field_key    TEXT NOT NULL,
    value_text   TEXT,
    file_ref     TEXT,
    needs_review INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(user_id, field_key)
);
"""
# ===============================================================

@contextmanager
def get_conn():
  """Commit on success, roll back on error, always close."""
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


def get_members_by_status(status):
  with get_conn() as conn:
    return conn.execute(
      "SELECT * FROM members WHERE status = ? ORDER BY updated_at DESC",
      (status,),
    ).fetchall()


def upsert_member(user_id, username=None, first_name=None, last_name=None):
  """Create if new, refresh Telegram details if not. Never touches status.
  COALESCE keeps existing values when an argument is omitted."""
  with get_conn() as conn:
    conn.execute(
      """
      INSERT INTO members (user_id, username, first_name, last_name)
      VALUES (?, ?, ?, ?)
      ON CONFLICT(user_id) DO UPDATE SET
        username   = COALESCE(excluded.username, members.username),
        first_name = COALESCE(excluded.first_name, members.first_name),
        last_name  = COALESCE(excluded.last_name, members.last_name),
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


def log_event(user_id, event, actor_user_id=None, note=None):
  """Append-only audit entry for anything that isn't a status change."""
  with get_conn() as conn:
    conn.execute(
      "INSERT INTO member_events (user_id, event, actor_user_id, note) "
      "VALUES (?, ?, ?, ?)",
      (user_id, event, actor_user_id, note),
    )


# --- applications ----------------------------------------------------
def create_application(user_id, summary_text, source_chat_id=None,
                       source_message_id=None):
  """Insert a new onboarding attempt; return its id."""
  with get_conn() as conn:
    cur = conn.execute(
      """
      INSERT INTO applications
        (user_id, summary_text, source_chat_id, source_message_id)
      VALUES (?, ?, ?, ?)
      """,
      (user_id, summary_text, source_chat_id, source_message_id),
    )
    return cur.lastrowid


def get_application(application_id):
  with get_conn() as conn:
    return conn.execute(
      "SELECT * FROM applications WHERE id = ?", (application_id,)
    ).fetchone()


def decide_application(application_id, decision, decided_by):
  """Record approve/decline. Returns False if already decided."""
  if decision not in ("approved", "declined"):
    raise ValueError(f"Unknown decision: {decision}")
  with get_conn() as conn:
    cur = conn.execute(
      """
      UPDATE applications
         SET decision = ?, decided_by = ?, decided_at = datetime('now')
       WHERE id = ? AND decision IS NULL
      """,
      (decision, decided_by, application_id),
    )
    return cur.rowcount == 1


# --- kyc -------------------------------------------------------------
def save_kyc_answer(user_id, field_key, value_text=None,
                    file_ref=None, needs_review=0):
  with get_conn() as conn:
    conn.execute(
      """
      INSERT INTO kyc_responses
        (user_id, field_key, value_text, file_ref, needs_review)
      VALUES (?, ?, ?, ?, ?)
      ON CONFLICT(user_id, field_key) DO UPDATE SET
        value_text   = excluded.value_text,
        file_ref     = excluded.file_ref,
        needs_review = excluded.needs_review,
        created_at   = datetime('now')
      """,
      (user_id, field_key, value_text, file_ref, needs_review),
    )


def get_kyc_answers(user_id):
  """Return {field_key: row} for one member."""
  with get_conn() as conn:
    rows = conn.execute(
      "SELECT * FROM kyc_responses WHERE user_id = ?", (user_id,)
    ).fetchall()
  return {r["field_key"]: r for r in rows}


def advance_kyc(user_id, next_index):
  """Move to the next field and reset the attempt counter together."""
  with get_conn() as conn:
    conn.execute(
      "UPDATE members SET kyc_field_index = ?, kyc_attempts = 0, "
      "updated_at = datetime('now') WHERE user_id = ?",
      (next_index, user_id),
    )


def bump_kyc_attempts(user_id):
  """Increment attempts on the current field; return the new count."""
  with get_conn() as conn:
    conn.execute(
      "UPDATE members SET kyc_attempts = kyc_attempts + 1, "
      "updated_at = datetime('now') WHERE user_id = ?",
      (user_id,),
    )
    row = conn.execute(
      "SELECT kyc_attempts FROM members WHERE user_id = ?", (user_id,)
    ).fetchone()
  return row["kyc_attempts"] if row else 0


def count_events(user_id, event):
  """How many times this event has been logged for a member."""
  with get_conn() as conn:
    row = conn.execute(
      "SELECT COUNT(*) AS n FROM member_events "
      "WHERE user_id = ? AND event = ?",
      (user_id, event),
    ).fetchone()
  return row["n"]


def seconds_since_last_event(user_id, event):
  """Seconds since the most recent matching event, or None if never.
  Computed in SQL so UTC/local time can't drift."""
  with get_conn() as conn:
    row = conn.execute(
      "SELECT (julianday('now') - julianday(created_at)) * 86400 AS secs "
      "FROM member_events WHERE user_id = ? AND event = ? "
      "ORDER BY id DESC LIMIT 1",
      (user_id, event),
    ).fetchone()
  return row["secs"] if row else None




# ==================================================
if __name__ == "__main__":
  init_db()
  print(f"Schema created / verified at {DB_PATH}")