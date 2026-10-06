"""Members, their status, and the audit trail (member_events)."""
from src.db.connection import get_conn
from src.db.schema import ALL_STATUSES, STATUS_ACTIVE, STATUS_REMOVED
# ===============================================================


def get_member(user_id):
  with get_conn() as conn:
    return conn.execute(
      "SELECT * FROM members WHERE user_id = ?", (user_id,)
    ).fetchone()


def is_removed(user_id):
  """Removed from ATL: expelled with /expel, or banned from the main group."""
  member = get_member(user_id)
  return member is not None and member["status"] == STATUS_REMOVED


def find_member_by_username(username_key):
  """The member whose stored Telegram username matches (case-insensitive)."""
  with get_conn() as conn:
    return conn.execute(
      "SELECT * FROM members WHERE lower(username) = ? "
      "ORDER BY updated_at DESC LIMIT 1",
      (username_key,),
    ).fetchone()


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


def refresh_telegram_details(user_id, username, first_name, last_name):
  """Make a known member's stored Telegram details exactly match what Alpha
  just saw, including clearing a username they've removed. A username
  belongs to one person at a time, so it's cleared from anyone else's row.
  Never creates a row and never touches status."""
  with get_conn() as conn:
    if username:
      conn.execute(
        "UPDATE members SET username = NULL, updated_at = datetime('now') "
        "WHERE lower(username) = lower(?) AND user_id != ?",
        (username, user_id),
      )
    conn.execute(
      "UPDATE members SET username = ?, first_name = ?, last_name = ?, "
      "updated_at = datetime('now') WHERE user_id = ?",
      (username, first_name, last_name, user_id),
    )


def upsert_active(conn, user_id, username, first_name, last_name):
  """Create or refresh a member and set them active. Runs inside the
  caller's transaction, so it's committed (or not) with everything else."""
  conn.execute(
    """
    INSERT INTO members (user_id, username, first_name, last_name, status)
    VALUES (?, ?, ?, ?, ?)
    ON CONFLICT(user_id) DO UPDATE SET
      username   = COALESCE(excluded.username, members.username),
      first_name = COALESCE(excluded.first_name, members.first_name),
      last_name  = COALESCE(excluded.last_name, members.last_name),
      status     = excluded.status,
      status_before_removal = NULL,
      updated_at = datetime('now')
    """,
    (user_id, username, first_name, last_name, STATUS_ACTIVE),
  )


def set_status(user_id, status, actor_user_id=None, note=None):
  """Change status and write the audit event in ONE transaction."""
  if status not in ALL_STATUSES:
    raise ValueError(f"Unknown status: {status}")
  with get_conn() as conn:
    if status == STATUS_REMOVED:
      # Remember what they were, so a reinstatement can't promote an
      # applicant to member (src/db/manage.py, reinstate_member).
      conn.execute(
        "UPDATE members SET status_before_removal = status "
        "WHERE user_id = ? AND status != ?",
        (user_id, STATUS_REMOVED),
      )
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
