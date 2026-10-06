"""Looking members up, expelling and reinstating them, and who may do it."""
from src.db.connection import get_conn
from src.db.schema import STATUS_ACTIVE, STATUS_PENDING_SUMMARY, STATUS_REMOVED
# ===========================================================================


# --- who may manage members ---------------------------------------------

def is_manager(user_id):
  """Only while they're active: being removed any way, not just by /expel
  (e.g. banned from the main group in Telegram), ends access to members' data."""
  with get_conn() as conn:
    return conn.execute(
      "SELECT 1 FROM member_managers g JOIN members m ON m.user_id = g.user_id "
      "WHERE g.user_id = ? AND m.status = ?",
      (user_id, STATUS_ACTIVE),
    ).fetchone() is not None


def grant_manager(user_id, granted_by):
  """Let an active member look members up and expel them. False if they
  already could or aren't active: this never changes anyone's status, so it
  can't bring back an expelled member or skip an applicant past induction."""
  with get_conn() as conn:
    cur = conn.execute(
      "INSERT OR IGNORE INTO member_managers (user_id, granted_by) "
      "SELECT user_id, ? FROM members WHERE user_id = ? AND status = ?",
      (granted_by, user_id, STATUS_ACTIVE),
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
      SELECT g.user_id, m.username, m.first_name, m.last_name, m.status
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
      "UPDATE members SET status = ?, status_before_removal = ?, "
      "updated_at = datetime('now') WHERE user_id = ?",
      (STATUS_REMOVED, member["status"], user_id),
    )
    conn.execute(
      "INSERT INTO member_events (user_id, event, actor_user_id, note) VALUES (?, ?, ?, ?)",
      (user_id, f"status:{STATUS_REMOVED}", actor_user_id,
       f"expelled (was {member['status']})"),
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
  """Owner's undo. Returns the status they're restored to, or None if they
  weren't removed.

  Only someone who was an active member goes back to active. Anyone removed
  mid-onboarding starts onboarding again from the induction group: reinstating
  must never skip induction review or KYC, and their application, vouch
  request and review card were all closed when they were expelled, so putting
  them back mid-flow would leave them stuck.

  status_before_removal is NULL for people removed before it was recorded.
  Back then the only ways to be removed (a ban noticed in the main group, or
  Alpha finding them banned) applied to active members only, so NULL means
  they were active."""
  with get_conn() as conn:
    member = conn.execute(
      "SELECT status, status_before_removal FROM members WHERE user_id = ?",
      (user_id,),
    ).fetchone()
    if member is None or member["status"] != STATUS_REMOVED:
      return None
    was = member["status_before_removal"] or STATUS_ACTIVE
    restored = STATUS_ACTIVE if was == STATUS_ACTIVE else STATUS_PENDING_SUMMARY
    conn.execute(
      "UPDATE members SET status = ?, status_before_removal = NULL, "
      "updated_at = datetime('now') WHERE user_id = ?",
      (restored, user_id),
    )
    conn.execute(
      "INSERT INTO member_events (user_id, event, actor_user_id, note) VALUES (?, ?, ?, ?)",
      (user_id, f"status:{restored}", actor_user_id,
       f"reinstated by the owner (was {was} before removal)"),
    )
  return restored
