import sqlite3
from contextlib import contextmanager

from config import DB_PATH

SCHEMA = """
  CREATE TABLE IF NOT EXISTS members (
    user_id    INTEGER PRIMARY KEY,
    full_name  TEXT,
    username   TEXT,
    first_seen TEXT NOT NULL DEFAULT (datetime('now'))
  );
  
  CREATE TABLE IF NOT EXISTS registrations (
    user_id       INTEGER NOT NULL,
    module        TEXT    NOT NULL,
    registered_at TEXT    NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (user_id, module),
    FOREIGN KEY (user_id) REFERENCES members(user_id)
  );
"""


@contextmanager
def get_conn():
  conn = sqlite3.connect(DB_PATH)
  conn.row_factory = sqlite3.Row
  try:
    yield conn
    conn.commit()
  finally:
    conn.close()


def init_db():
  with get_conn() as conn:
    conn.executescript(SCHEMA)


def upsert_member(user_id, full_name, username):
  with get_conn() as conn:
    conn.execute(
      """
      INSERT INTO members (user_id, full_name, username)
      VALUES (?, ?, ?)
      ON CONFLICT(user_id) DO UPDATE SET
        full_name = excluded.full_name,
        username  = excluded.username
      """,
      (user_id, full_name, username),
    )


def register_module(user_id, module):
  with get_conn() as conn:
    conn.execute(
      "INSERT OR IGNORE INTO registrations (user_id, module) VALUES (?, ?)",
      (user_id, module),
    )


def get_module_member_ids(module):
  with get_conn() as conn:
    rows = conn.execute(
      "SELECT user_id FROM registrations WHERE module = ?", (module,)
    ).fetchall()
  return [row["user_id"] for row in rows]