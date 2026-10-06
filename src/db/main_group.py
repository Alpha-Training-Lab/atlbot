"""Keeping the main ATL group and the members table in step: owner-added
members, per-member invite links, and the in-group "update your details"
nudge."""
from src.db.connection import get_conn
from src.db.members import upsert_active
from src.db.schema import STATUS_ACTIVE
# ===============================================================


def activate_by_owner(user_id, username, first_name, last_name):
  """The owner added this person to the main group directly: the one
  sanctioned backdoor. Active whatever their previous status, expelled
  included: adding them by hand is the owner's explicit choice."""
  with get_conn() as conn:
    member = conn.execute(
      "SELECT status FROM members WHERE user_id = ?", (user_id,)
    ).fetchone()
    upsert_active(conn, user_id, username, first_name, last_name)
    if member is None or member["status"] != STATUS_ACTIVE:
      conn.execute(
        "INSERT INTO member_events (user_id, event, note) VALUES (?, ?, ?)",
        (user_id, f"status:{STATUS_ACTIVE}",
         f"added to the main group by the owner (was {member['status'] if member else 'new'})"),
      )


# --- invites ---------------------------------------------------------
def get_invite(user_id):
  """The member's stored invite link, if it has more than 5 minutes left."""
  with get_conn() as conn:
    row = conn.execute(
      "SELECT invite_link FROM main_group_invites "
      "WHERE user_id = ? AND expires_at > datetime('now', '+5 minutes')",
      (user_id,),
    ).fetchone()
  return row["invite_link"] if row else None


def save_invite(user_id, invite_link, seconds):
  with get_conn() as conn:
    conn.execute(
      """
      INSERT INTO main_group_invites (user_id, invite_link, expires_at)
      VALUES (?, ?, datetime('now', ?))
      ON CONFLICT(user_id) DO UPDATE SET
        invite_link = excluded.invite_link, expires_at = excluded.expires_at
      """,
      (user_id, invite_link, f"+{int(seconds)} seconds"),
    )


def clear_invite(user_id):
  """Called when they join: a one-use link is spent once used."""
  with get_conn() as conn:
    conn.execute("DELETE FROM main_group_invites WHERE user_id = ?", (user_id,))


# --- in-group nudges -------------------------------------------------
def should_prompt(user_id, every_days):
  with get_conn() as conn:
    row = conn.execute(
      "SELECT 1 FROM group_prompts WHERE user_id = ? "
      "AND prompted_at > datetime('now', ?)",
      (user_id, f"-{int(every_days)} days"),
    ).fetchone()
  return row is None


def mark_prompted(user_id):
  with get_conn() as conn:
    conn.execute(
      "INSERT INTO group_prompts (user_id) VALUES (?) "
      "ON CONFLICT(user_id) DO UPDATE SET prompted_at = datetime('now')",
      (user_id,),
    )
