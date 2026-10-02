"""Onboarding records: induction applications, KYC answers and progress, and
the induction-group "tap to register" prompt."""
from src.db.connection import get_conn
# ===============================================================


# --- applications ----------------------------------------------------
def create_application(user_id, summary_text, source_chat_id=None,
                       source_message_id=None):
  """Insert a new onboarding attempt; return its id."""
  with get_conn() as conn:
    cur = conn.execute(
      """
      INSERT INTO applications
        (user_id, summary_text, source_chat_id, source_message_id)
      VALUES (?, ?, ?, ?)
      """,
      (user_id, summary_text, source_chat_id, source_message_id),
    )
    return cur.lastrowid


def get_application(application_id):
  with get_conn() as conn:
    return conn.execute(
      "SELECT * FROM applications WHERE id = ?", (application_id,)
    ).fetchone()


def decide_application(application_id, decision, decided_by):
  """Record approve/decline. Returns False if already decided."""
  if decision not in ("approved", "declined"):
    raise ValueError(f"Unknown decision: {decision}")
  with get_conn() as conn:
    cur = conn.execute(
      """
      UPDATE applications
         SET decision = ?, decided_by = ?, decided_at = datetime('now')
       WHERE id = ? AND decision IS NULL
      """,
      (decision, decided_by, application_id),
    )
    return cur.rowcount == 1


# --- kyc -------------------------------------------------------------
def save_kyc_answer(user_id, field_key, value_text=None,
                    file_ref=None, needs_review=0):
  with get_conn() as conn:
    conn.execute(
      """
      INSERT INTO kyc_responses
        (user_id, field_key, value_text, file_ref, needs_review)
      VALUES (?, ?, ?, ?, ?)
      ON CONFLICT(user_id, field_key) DO UPDATE SET
        value_text   = excluded.value_text,
        file_ref     = excluded.file_ref,
        needs_review = excluded.needs_review,
        created_at   = datetime('now')
      """,
      (user_id, field_key, value_text, file_ref, needs_review),
    )


def get_kyc_answers(user_id):
  """Return {field_key: row} for one member."""
  with get_conn() as conn:
    rows = conn.execute(
      "SELECT * FROM kyc_responses WHERE user_id = ?", (user_id,)
    ).fetchall()
  return {r["field_key"]: r for r in rows}


def advance_kyc(user_id, next_index):
  """Move to the next field and reset the attempt counter together."""
  with get_conn() as conn:
    conn.execute(
      "UPDATE members SET kyc_field_index = ?, kyc_attempts = 0, "
      "updated_at = datetime('now') WHERE user_id = ?",
      (next_index, user_id),
    )


def bump_kyc_attempts(user_id):
  """Increment attempts on the current field; return the new count."""
  with get_conn() as conn:
    conn.execute(
      "UPDATE members SET kyc_attempts = kyc_attempts + 1, "
      "updated_at = datetime('now') WHERE user_id = ?",
      (user_id,),
    )
    row = conn.execute(
      "SELECT kyc_attempts FROM members WHERE user_id = ?", (user_id,)
    ).fetchone()
  return row["kyc_attempts"] if row else 0


# --- registration prompts --------------------------------------------
def save_registration_prompt(user_id, chat_id, message_id):
  with get_conn() as conn:
    conn.execute(
      """
      INSERT INTO registration_prompts (user_id, chat_id, message_id)
      VALUES (?, ?, ?)
      ON CONFLICT(user_id) DO UPDATE SET
        chat_id = excluded.chat_id, message_id = excluded.message_id
      """,
      (user_id, chat_id, message_id),
    )


def pop_registration_prompt(user_id):
  """Return and forget the member's registration prompt, including its
  timed fallback deletion, so the sweep doesn't try to delete it twice."""
  with get_conn() as conn:
    row = conn.execute(
      "SELECT chat_id, message_id FROM registration_prompts WHERE user_id = ?",
      (user_id,),
    ).fetchone()
    if row is None:
      return None
    conn.execute("DELETE FROM registration_prompts WHERE user_id = ?", (user_id,))
    conn.execute(
      "DELETE FROM scheduled_deletions WHERE chat_id = ? AND message_id = ?",
      (row["chat_id"], row["message_id"]),
    )
  return row
