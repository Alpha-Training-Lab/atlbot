"""Messages queued for deletion. src/common/cleanup.py deletes them when due."""
from src.db.connection import get_conn
# ===============================================================


def schedule_deletion(chat_id, message_id, seconds):
  with get_conn() as conn:
    conn.execute(
      "INSERT INTO scheduled_deletions (chat_id, message_id, delete_at) "
      "VALUES (?, ?, datetime('now', ?))",
      (chat_id, message_id, f"+{int(seconds)} seconds"),
    )


def due_deletions(limit=50):
  with get_conn() as conn:
    return conn.execute(
      "SELECT * FROM scheduled_deletions "
      "WHERE delete_at <= datetime('now') ORDER BY delete_at LIMIT ?",
      (limit,),
    ).fetchall()


def clear_deletion(row_id):
  with get_conn() as conn:
    conn.execute("DELETE FROM scheduled_deletions WHERE id = ?", (row_id,))
