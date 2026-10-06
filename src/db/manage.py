"""Looking members up, expelling and reinstating them, and who may do it."""
from src.db.connection import get_conn
from src.db.members import upsert_active
from src.db.schema import STATUS_ACTIVE, STATUS_REMOVED
# ===========================================================================


# --- who may manage members ---------------------------------------------

def is_manager(user_id):
  with get_conn() as conn:
    return conn.execute(
      "SELECT 1 FROM member_managers WHERE user_id = ?", (user_id,)
    ).fetchone() is not None


def grant_manager(user_id, username, first_name, last_name, granted_by):
  """Let someone look members up and expel them. False if they already could."""
  with get_conn() as conn:
    upsert_active(conn, user_id, username, first_name, last_name)
    cur = conn.execute(
      "INSERT OR IGNORE INTO member_managers (user_id, granted_by) VALUES (?, ?)",
      (user_id, granted_by),
    )
    if cur.rowcount:
      conn.execute(
        "INSERT INTO member_events (user_id, event, actor_user_id) VALUES (?, ?, ?)",
        (user_id, "manager_granted", granted_by),
      )
  return cur.rowcount > 0


def revoke_manager(user_id, revoked_by):
  with get_conn() as conn:
    cur = conn.execute("DELETE FROM member_managers WHERE user_id = ?", (user_id,))
    if cur.rowcount:
      conn.execute(
        "INSERT INTO member_events (user_id, event, actor_user_id) VALUES (?, ?, ?)",
        (user_id, "manager_revoked", revoked_by),
      )
  return cur.rowcount > 0


def list_managers():
  with get_conn() as conn:
    return conn.execute(
      """
      SELECT g.user_id, m.username, m.first_name, m.last_name
      FROM member_managers g JOIN members m ON m.user_id = g.user_id
      ORDER BY lower(COALESCE(m.username, m.first_name, ''))
      """
    ).fetchall()


# --- finding members --------------------------------------------------------

def search_members(text, limit=10):
  """Members whose Telegram name, username or KYC full name contains text."""
  like = f"%{text.strip().lower()}%"
  with get_conn() as conn:
    return conn.execute(
      """
      SELECT DISTINCT m.user_id, m.username, m.first_name, m.last_name, m.status
      FROM members m
      LEFT JOIN kyc_responses k ON k.user_id = m.user_id AND k.field_key = 'full_name'
      WHERE lower(COALESCE(m.username, '')) LIKE ?
         OR lower(TRIM(COALESCE(m.first_name, '') || ' ' || COALESCE(m.last_name, ''))) LIKE ?
         OR lower(COALESCE(k.value_text, '')) LIKE ?
      ORDER BY lower(COALESCE(m.username, m.first_name, ''))
      LIMIT ?
      """,
      (like, like, like, limit),
    ).fetchall()


def member_summary(user_id):
  """Role, Legacy Member, manager, and when they joined, for a lookup card."""
  with get_conn() as conn:
    role = conn.execute(
      "SELECT role FROM member_roles WHERE user_id = ?", (user_id,)).fetchone()
    special = conn.execute(
      "SELECT 1 FROM special_members WHERE user_id = ?", (user_id,)).fetchone()
    manager = conn.execute(
      "SELECT 1 FROM member_managers WHERE user_id = ?", (user_id,)).fetchone()
    # The latest expulsion, only while they're still removed (not once reinstated).
    expelled = conn.execute(
      "SELECT e.note, e.created_at FROM member_events e "
      "JOIN members m ON m.user_id = e.user_id AND m.status = 'removed' "
      "WHERE e.user_id = ? AND e.event = 'expelled' ORDER BY e.id DESC LIMIT 1",
      (user_id,)).fetchone()
  return {
    "role": role["role"] if role else None,
    "legacy_member": special is not None,
    "manager": manager is not None,
    "expelled": expelled,
  }


# --- expelling and reinstating ---------------------------------------------

def expel_member(user_id, actor_user_id, reason):
  """Mark a member removed, in ONE transaction: status, the audit trail with
  the reason, and every route that could make them active again (role,
  manager access, Legacy Member list, open vouch requests, open induction
  applications, open sessions).
  Returns False if they weren't on record or were already removed."""
  with get_conn() as conn:
    member = conn.execute(
      "SELECT status, username FROM members WHERE user_id = ?", (user_id,)
    ).fetchone()
    if member is None or member["status"] == STATUS_REMOVED:
      return False
    conn.execute(
      "UPDATE members SET status = ?, updated_at = datetime('now') WHERE user_id = ?",
      (STATUS_REMOVED, user_id),
    )
    conn.execute(
      "INSERT INTO member_events (user_id, event, actor_user_id, note) VALUES (?, ?, ?, ?)",
      (user_id, f"status:{STATUS_REMOVED}", actor_user_id, "expelled"),
    )
    conn.execute(
      "INSERT INTO member_events (user_id, event, actor_user_id, note) VALUES (?, ?, ?, ?)",
      (user_id, "expelled", actor_user_id, reason),
    )
    conn.execute("DELETE FROM member_roles WHERE user_id = ?", (user_id,))
    conn.execute("DELETE FROM member_managers WHERE user_id = ?", (user_id,))
    conn.execute("DELETE FROM special_members WHERE user_id = ?", (user_id,))
    if member["username"]:
      conn.execute(
        "DELETE FROM special_members WHERE user_id IS NULL AND username_key = ?",
        (member["username"].lower(),),
      )
    conn.execute(
      "UPDATE vouch_requests SET status = 'cancelled', decided_at = datetime('now') "
      "WHERE user_id = ? AND status = 'pending'",
      (user_id,),
    )
    # Otherwise Approve/Decline on their old induction card would move them
    # out of removed: the card's buttons only check the application is open.
    conn.execute(
      "UPDATE applications SET decision = 'declined', decided_by = ?, "
      "decided_at = datetime('now') WHERE user_id = ? AND decision IS NULL",
      (actor_user_id, user_id),
    )
    conn.execute("DELETE FROM profile_sessions WHERE user_id = ?", (user_id,))
    conn.execute("DELETE FROM edit_sessions WHERE user_id = ?", (user_id,))
  return True


def reinstate_member(user_id, actor_user_id):
  """Owner's undo: active again. False if they weren't removed."""
  with get_conn() as conn:
    member = conn.execute(
      "SELECT status FROM members WHERE user_id = ?", (user_id,)
    ).fetchone()
    if member is None or member["status"] != STATUS_REMOVED:
      return False
    conn.execute(
      "UPDATE members SET status = ?, updated_at = datetime('now') WHERE user_id = ?",
      (STATUS_ACTIVE, user_id),
    )
    conn.execute(
      "INSERT INTO member_events (user_id, event, actor_user_id, note) VALUES (?, ?, ?, ?)",
      (user_id, f"status:{STATUS_ACTIVE}", actor_user_id, "reinstated by the owner"),
    )
  return True
