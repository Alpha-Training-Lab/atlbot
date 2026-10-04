"""The owner's special list ("Legacy Members"): active without onboarding.

Callers pass usernames already normalised (db.username_key)."""
from src.db.connection import get_conn
from src.db.members import upsert_active
from src.db.schema import STATUS_ACTIVE
# ===========================================================================

_NOTE = "added as a Legacy Member by the owner"


def _activate(conn, user_id, username, first_name, last_name, added_by):
  member = conn.execute(
    "SELECT status FROM members WHERE user_id = ?", (user_id,)
  ).fetchone()
  upsert_active(conn, user_id, username, first_name, last_name)
  if member is None or member["status"] != STATUS_ACTIVE:
    conn.execute(
      "INSERT INTO member_events (user_id, event, actor_user_id, note) "
      "VALUES (?, ?, ?, ?)",
      (user_id, f"status:{STATUS_ACTIVE}", added_by, _NOTE),
    )


def add_special(user_id, username, first_name, last_name, username_key, added_by):
  """Add someone whose Telegram id is known: their row is created or
  refreshed, set active whatever it was (the owner's explicit choice), and
  tagged. Any waiting entry for their username is used up. Returns False if
  they were already on the list."""
  with get_conn() as conn:
    already = conn.execute(
      "SELECT 1 FROM special_members WHERE user_id = ?", (user_id,)
    ).fetchone()
    _activate(conn, user_id, username, first_name, last_name, added_by)
    if username_key:
      conn.execute(
        "DELETE FROM special_members WHERE user_id IS NULL AND username_key = ?",
        (username_key,),
      )
    if already:
      return False
    conn.execute(
      "INSERT INTO special_members (user_id, username_key, added_by, linked_at) "
      "VALUES (?, ?, ?, datetime('now'))",
      (user_id, username_key, added_by),
    )
    conn.execute(
      "INSERT INTO member_events (user_id, event, actor_user_id) VALUES (?, ?, ?)",
      (user_id, "special_member_added", added_by),
    )
  return True


def add_special_waiting(username_key, added_by):
  """Remember a typed username until Alpha sees that person. Returns False
  if it was already waiting."""
  with get_conn() as conn:
    cur = conn.execute(
      "INSERT OR IGNORE INTO special_members (username_key, added_by) VALUES (?, ?)",
      (username_key, added_by),
    )
  return cur.rowcount > 0


def claim_special(user_id, username, first_name, last_name, username_key):
  """Alpha has just seen this person. If the owner put their username on
  the list, link their id, make them active and tag them. True if so."""
  if not username_key:
    return False
  with get_conn() as conn:
    row = conn.execute(
      "SELECT id, added_by FROM special_members "
      "WHERE user_id IS NULL AND username_key = ?",
      (username_key,),
    ).fetchone()
    if row is None:
      return False
    if conn.execute(
        "SELECT 1 FROM special_members WHERE user_id = ?", (user_id,)
    ).fetchone():
      conn.execute("DELETE FROM special_members WHERE id = ?", (row["id"],))
      return False   # already on the list under their id
    _activate(conn, user_id, username, first_name, last_name, row["added_by"])
    conn.execute(
      "UPDATE special_members SET user_id = ?, linked_at = datetime('now') "
      "WHERE id = ?",
      (user_id, row["id"]),
    )
    conn.execute(
      "INSERT INTO member_events (user_id, event, actor_user_id, note) "
      "VALUES (?, ?, ?, ?)",
      (user_id, "special_member_added", row["added_by"], "linked on first sight"),
    )
  return True


def is_special(user_id):
  with get_conn() as conn:
    return conn.execute(
      "SELECT 1 FROM special_members WHERE user_id = ?", (user_id,)
    ).fetchone() is not None


def list_specials():
  """(linked rows with the member's details, waiting usernames)."""
  with get_conn() as conn:
    linked = conn.execute(
      """
      SELECT s.user_id, m.username, m.first_name, m.last_name, m.status, s.linked_at
      FROM special_members s JOIN members m ON m.user_id = s.user_id
      ORDER BY lower(COALESCE(m.username, m.first_name, ''))
      """
    ).fetchall()
    waiting = [r["username_key"] for r in conn.execute(
      "SELECT username_key FROM special_members WHERE user_id IS NULL "
      "ORDER BY username_key"
    )]
  return linked, waiting


def remove_special(username_key):
  """Take someone off the list by username (linked or waiting). Their status
  is left alone. Returns how many entries were removed."""
  with get_conn() as conn:
    linked = conn.execute(
      "SELECT s.user_id FROM special_members s JOIN members m ON m.user_id = s.user_id "
      "WHERE lower(m.username) = ?",
      (username_key,),
    ).fetchall()
    n = 0
    for row in linked:
      conn.execute("DELETE FROM special_members WHERE user_id = ?", (row["user_id"],))
      conn.execute(
        "INSERT INTO member_events (user_id, event) VALUES (?, ?)",
        (row["user_id"], "special_member_removed"),
      )
      n += 1
    n += conn.execute(
      "DELETE FROM special_members WHERE user_id IS NULL AND username_key = ?",
      (username_key,),
    ).rowcount
  return n
