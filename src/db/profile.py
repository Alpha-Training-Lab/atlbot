"""Profile flows for active members: filling in missing details
(profile_sessions), editing details on file (edit_sessions), and details
held for admin approval (pending_changes)."""
import json

from src.db.connection import get_conn
# ===============================================================


# --- profile sessions (fill in what's missing) -----------------------
def get_profile_session(user_id):
  with get_conn() as conn:
    return conn.execute(
      "SELECT * FROM profile_sessions WHERE user_id = ?", (user_id,)
    ).fetchone()


def start_profile_session(user_id, field_keys):
  """Start (or restart) a session that will ask these fields, in order."""
  with get_conn() as conn:
    conn.execute(
      """
      INSERT INTO profile_sessions (user_id, field_keys)
      VALUES (?, ?)
      ON CONFLICT(user_id) DO UPDATE SET
        field_keys      = excluded.field_keys,
        position        = 0,
        attempts        = 0,
        card_message_id = NULL,
        started_at      = datetime('now')
      """,
      (user_id, ",".join(field_keys)),
    )


def advance_profile_session(user_id):
  with get_conn() as conn:
    conn.execute(
      "UPDATE profile_sessions SET position = position + 1, attempts = 0 "
      "WHERE user_id = ?",
      (user_id,),
    )


def bump_profile_attempts(user_id):
  with get_conn() as conn:
    conn.execute(
      "UPDATE profile_sessions SET attempts = attempts + 1 WHERE user_id = ?",
      (user_id,),
    )
    row = conn.execute(
      "SELECT attempts FROM profile_sessions WHERE user_id = ?", (user_id,)
    ).fetchone()
  return row["attempts"] if row else 0


def set_profile_card(user_id, card_message_id):
  with get_conn() as conn:
    conn.execute(
      "UPDATE profile_sessions SET card_message_id = ? WHERE user_id = ?",
      (card_message_id, user_id),
    )


def end_profile_session(user_id):
  with get_conn() as conn:
    conn.execute("DELETE FROM profile_sessions WHERE user_id = ?", (user_id,))


# --- pending changes (held for admin approval) -----------------------
def add_pending_change(user_id, field_key, value_text=None, file_ref=None,
                       card_message_id=None):
  """Hold a detail for admin approval. Replaces any undecided request for
  the same field. Returns the new row id."""
  with get_conn() as conn:
    conn.execute(
      "DELETE FROM pending_changes "
      "WHERE user_id = ? AND field_key = ? AND decision IS NULL",
      (user_id, field_key),
    )
    cur = conn.execute(
      "INSERT INTO pending_changes "
      "(user_id, field_key, value_text, file_ref, card_message_id) "
      "VALUES (?, ?, ?, ?, ?)",
      (user_id, field_key, value_text, file_ref, card_message_id),
    )
    conn.execute(
      "INSERT INTO member_events (user_id, event, note) VALUES (?, ?, ?)",
      (user_id, "profile_change_requested", field_key),
    )
    return cur.lastrowid


def attach_change_to_card(change_id, card_message_id):
  with get_conn() as conn:
    conn.execute(
      "UPDATE pending_changes SET card_message_id = ? WHERE id = ?",
      (card_message_id, change_id),
    )


def get_change(change_id):
  with get_conn() as conn:
    return conn.execute(
      "SELECT * FROM pending_changes WHERE id = ?", (change_id,)
    ).fetchone()


def get_open_changes(user_id):
  with get_conn() as conn:
    return conn.execute(
      "SELECT * FROM pending_changes WHERE user_id = ? AND decision IS NULL",
      (user_id,),
    ).fetchall()


def get_card_changes(card_message_id):
  with get_conn() as conn:
    return conn.execute(
      "SELECT * FROM pending_changes WHERE card_message_id = ? ORDER BY id",
      (card_message_id,),
    ).fetchall()


def _apply_change(conn, row):
  """Write an approved pending change into kyc_responses."""
  conn.execute(
    """
    INSERT INTO kyc_responses
      (user_id, field_key, value_text, file_ref, needs_review)
    VALUES (?, ?, ?, ?, 0)
    ON CONFLICT(user_id, field_key) DO UPDATE SET
      value_text   = excluded.value_text,
      file_ref     = excluded.file_ref,
      needs_review = 0,
      created_at   = datetime('now')
    """,
    (row["user_id"], row["field_key"], row["value_text"], row["file_ref"]),
  )


def decide_change(change_id, decision, admin_id, admin_name):
  """Approve or reject one held detail, in ONE transaction. Approving writes
  it into kyc_responses. The audit trail records which field and who
  decided, never the value itself. Returns False if already decided."""
  if decision not in ("approved", "rejected"):
    raise ValueError(f"Unknown decision: {decision}")
  with get_conn() as conn:
    cur = conn.execute(
      "UPDATE pending_changes SET decision = ?, decided_by = ?, "
      "decided_by_name = ?, decided_at = datetime('now') "
      "WHERE id = ? AND decision IS NULL",
      (decision, admin_id, admin_name, change_id),
    )
    if cur.rowcount == 0:
      return False   # someone else got there first
    row = conn.execute(
      "SELECT * FROM pending_changes WHERE id = ?", (change_id,)
    ).fetchone()
    if decision == "approved":
      _apply_change(conn, row)
    conn.execute(
      "INSERT INTO member_events (user_id, event, actor_user_id, note) "
      "VALUES (?, ?, ?, ?)",
      (row["user_id"], f"profile_change_{decision}", admin_id, row["field_key"]),
    )
  return True


def decide_card_changes(card_message_id, decision, admin_id, admin_name,
                        reason=None):
  """Approve or reject every undecided change on one card, in ONE
  transaction, so a grouped edit (e.g. a whole ID) can never be half-applied.
  Returns the rows decided; empty if the card was already decided."""
  if decision not in ("approved", "rejected"):
    raise ValueError(f"Unknown decision: {decision}")
  with get_conn() as conn:
    rows = conn.execute(
      "SELECT * FROM pending_changes "
      "WHERE card_message_id = ? AND decision IS NULL",
      (card_message_id,),
    ).fetchall()
    for row in rows:
      conn.execute(
        "UPDATE pending_changes SET decision = ?, decided_by = ?, "
        "decided_by_name = ?, decided_at = datetime('now') WHERE id = ?",
        (decision, admin_id, admin_name, row["id"]),
      )
      if decision == "approved":
        _apply_change(conn, row)
      note = row["field_key"] + (f"; reason: {reason}" if reason else "")
      conn.execute(
        "INSERT INTO member_events (user_id, event, actor_user_id, note) "
        "VALUES (?, ?, ?, ?)",
        (row["user_id"], f"profile_change_{decision}", admin_id, note),
      )
  return rows


# --- edit sessions (change details already on file) ------------------
def get_edit_session(user_id):
  with get_conn() as conn:
    return conn.execute(
      "SELECT * FROM edit_sessions WHERE user_id = ?", (user_id,)
    ).fetchone()


def start_edit_session(user_id, group_key):
  with get_conn() as conn:
    conn.execute(
      """
      INSERT INTO edit_sessions (user_id, group_key) VALUES (?, ?)
      ON CONFLICT(user_id) DO UPDATE SET
        group_key  = excluded.group_key,
        position   = 0,
        attempts   = 0,
        draft      = '{}',
        started_at = datetime('now')
      """,
      (user_id, group_key),
    )


def save_edit_draft(user_id, field_key, value_text=None, file_ref=None):
  """Hold one new value and move to the next field of the group."""
  with get_conn() as conn:
    row = conn.execute(
      "SELECT draft FROM edit_sessions WHERE user_id = ?", (user_id,)
    ).fetchone()
    if row is None:
      return
    draft = json.loads(row["draft"])
    draft[field_key] = {"value_text": value_text, "file_ref": file_ref}
    conn.execute(
      "UPDATE edit_sessions SET draft = ?, position = position + 1, "
      "attempts = 0 WHERE user_id = ?",
      (json.dumps(draft), user_id),
    )


def bump_edit_attempts(user_id):
  with get_conn() as conn:
    conn.execute(
      "UPDATE edit_sessions SET attempts = attempts + 1 WHERE user_id = ?",
      (user_id,),
    )
    row = conn.execute(
      "SELECT attempts FROM edit_sessions WHERE user_id = ?", (user_id,)
    ).fetchone()
  return row["attempts"] if row else 0


def end_edit_session(user_id):
  with get_conn() as conn:
    conn.execute("DELETE FROM edit_sessions WHERE user_id = ?", (user_id,))
