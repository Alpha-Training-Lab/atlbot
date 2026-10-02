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


def init_db():
  with get_conn() as conn:
    conn.executescript(SCHEMA)
