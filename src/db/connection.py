"""Opening the database. Every query in src/db goes through get_conn()."""
import sqlite3
from contextlib import contextmanager

from src.config import DB_PATH
from src.db.schema import SCHEMA
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


# Columns added after their table first shipped. CREATE TABLE IF NOT EXISTS
# never changes a table that already exists, so they're added here, once:
# (table, column, type, value for rows that already exist).
_ADDED_COLUMNS = [
  # Existing sessions start their idle clock at the upgrade, not at the
  # session's start, so nobody mid-answer is closed straight away.
  ("profile_sessions", "last_active_at", "TEXT", "datetime('now')"),
  ("edit_sessions", "last_active_at", "TEXT", "datetime('now')"),
  # Only matters for a database made by an early test of the weekly brief.
  ("brief_days", "senders_json", "TEXT", "'[]'"),
]


def init_db():
  with get_conn() as conn:
    conn.executescript(SCHEMA)
    for table, column, kind, backfill in _ADDED_COLUMNS:
      have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
      if column not in have:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")
        conn.execute(f"UPDATE {table} SET {column} = {backfill}")
