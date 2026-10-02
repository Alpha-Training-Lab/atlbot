"""Vouch consent requests: a member names a vouch, the vouch confirms."""
from src.db.connection import get_conn
# ===========================================================================

OUTCOMES = ("yes", "no", "expired", "not_member", "cancelled")


def create_vouch_request(user_id, vouch_key, purpose, card_message_id=None):
  """Open a new request, cancelling any older open one for the same member
  and purpose (they restarted registration, or changed their vouch)."""
  with get_conn() as conn:
    conn.execute(
      "UPDATE vouch_requests SET status = 'cancelled', decided_at = datetime('now') "
      "WHERE user_id = ? AND purpose = ? AND status = 'pending'",
      (user_id, purpose),
    )
    cur = conn.execute(
      "INSERT INTO vouch_requests (user_id, vouch_key, purpose, card_message_id) "
      "VALUES (?, ?, ?, ?)",
      (user_id, vouch_key, purpose, card_message_id),
    )
    conn.execute(
      "INSERT INTO member_events (user_id, event, note) VALUES (?, ?, ?)",
      (user_id, "vouch_requested", f"request {cur.lastrowid}; {purpose}"),
    )
    return cur.lastrowid


def get_vouch_request(request_id):
  with get_conn() as conn:
    return conn.execute(
      "SELECT * FROM vouch_requests WHERE id = ?", (request_id,)
    ).fetchone()


def latest_vouch_request(user_id, purpose):
  with get_conn() as conn:
    return conn.execute(
      "SELECT * FROM vouch_requests WHERE user_id = ? AND purpose = ? "
      "ORDER BY id DESC LIMIT 1",
      (user_id, purpose),
    ).fetchone()


def open_requests_for_vouch(vouch_key):
  with get_conn() as conn:
    return conn.execute(
      """
      SELECT *,
        (julianday('now') - julianday(COALESCE(last_nudged_at, created_at)))
          * 86400 AS since_nudge_seconds
      FROM vouch_requests WHERE vouch_key = ? AND status = 'pending' ORDER BY id
      """,
      (vouch_key,),
    ).fetchall()


def open_vouch_requests_with_age():
  """Every open request, with seconds since it opened and since the vouch
  was last contacted (julianday, like the rest of the time arithmetic)."""
  with get_conn() as conn:
    return conn.execute(
      """
      SELECT *,
        (julianday('now') - julianday(created_at)) * 86400 AS age_seconds,
        (julianday('now') - julianday(COALESCE(last_nudged_at, created_at)))
          * 86400 AS since_nudge_seconds
      FROM vouch_requests WHERE status = 'pending' ORDER BY id
      """
    ).fetchall()


def set_vouch_contact(request_id, vouch_user_id):
  with get_conn() as conn:
    conn.execute(
      "UPDATE vouch_requests SET vouch_user_id = ? WHERE id = ?",
      (vouch_user_id, request_id),
    )


def mark_vouch_nudged(request_id):
  with get_conn() as conn:
    conn.execute(
      "UPDATE vouch_requests SET last_nudged_at = datetime('now') WHERE id = ?",
      (request_id,),
    )


def decide_vouch_request(request_id, outcome):
  """Close an open request. False if it was already closed, so two paths
  (a tap and the expiry sweep, say) can never both act on it."""
  if outcome not in OUTCOMES:
    raise ValueError(f"Unknown vouch outcome: {outcome}")
  with get_conn() as conn:
    cur = conn.execute(
      "UPDATE vouch_requests SET status = ?, decided_at = datetime('now') "
      "WHERE id = ? AND status = 'pending'",
      (outcome, request_id),
    )
    if cur.rowcount == 0:
      return False
    row = conn.execute(
      "SELECT user_id FROM vouch_requests WHERE id = ?", (request_id,)
    ).fetchone()
    conn.execute(
      "INSERT INTO member_events (user_id, event, note) VALUES (?, ?, ?)",
      (row["user_id"], f"vouch_{outcome}", f"request {request_id}"),
    )
  return True


def cancel_vouch_requests(user_id, purpose):
  with get_conn() as conn:
    conn.execute(
      "UPDATE vouch_requests SET status = 'cancelled', decided_at = datetime('now') "
      "WHERE user_id = ? AND purpose = ? AND status = 'pending'",
      (user_id, purpose),
    )
