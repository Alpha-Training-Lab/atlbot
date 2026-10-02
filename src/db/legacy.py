"""Members imported from the old website's spreadsheet (legacy_members and
legacy_responses), and turning a verified main-group member into an active
member, with or without a spreadsheet row."""
import re

from src.db.connection import get_conn
from src.db.members import upsert_active
from src.db.schema import (STATUS_ACTIVE, STATUS_AWAITING_DM,
                           STATUS_KYC_IN_PROGRESS, STATUS_PENDING_SUMMARY)
# ===============================================================

# Statuses a legacy member may be promoted from. Anything else (an application
# an admin is still deciding, a decline, a removal) is left for a human.
_LEGACY_PROMOTABLE = (STATUS_PENDING_SUMMARY, STATUS_AWAITING_DM,
                      STATUS_KYC_IN_PROGRESS, STATUS_ACTIVE)

_USERNAME_RE = re.compile(r"[a-z0-9_]{5,32}")


# --- matching keys ---------------------------------------------------
# Used both when importing (scripts/import_legacy.py) and when matching a
# live Telegram user, so the two can never drift apart.
def username_key(raw):
  """'@Name', 'name', 't.me/name' -> 'name'. Anything invalid -> None."""
  v = (raw or "").strip().lower()
  v = re.sub(r"^(https?://)?(www\.)?(t\.me|telegram\.me)/", "", v)
  v = v.lstrip("@").strip()
  return v if _USERNAME_RE.fullmatch(v) else None


def phone_key(raw):
  """Last 10 digits, so 08031234567, +2348031234567 and 8031234567
  (Excel dropped the leading zero) all produce the same key."""
  digits = re.sub(r"\D", "", raw or "")
  return digits[-10:] if len(digits) >= 10 else None


# --- linking ---------------------------------------------------------
def find_legacy_match(column, value):
  """Return the legacy_id to link, or None.

  Safe only if the key appears on exactly one spreadsheet row and that row
  is unclaimed. Duplicates are left for a human to resolve."""
  # column goes into the SQL text, so it must come from this fixed list,
  # never from user input.
  if column not in ("username_key", "phone_key") or not value:
    return None
  with get_conn() as conn:
    rows = conn.execute(
      f"SELECT legacy_id, linked_user_id FROM legacy_members WHERE {column} = ?",
      (value,),
    ).fetchall()
  if len(rows) != 1 or rows[0]["linked_user_id"] is not None:
    return None
  return rows[0]["legacy_id"]


def is_legacy_linked(user_id):
  with get_conn() as conn:
    return conn.execute(
      "SELECT 1 FROM legacy_members WHERE linked_user_id = ?", (user_id,)
    ).fetchone() is not None


def link_legacy(legacy_id, user_id, username, first_name, last_name, method):
  """Claim a spreadsheet row for a Telegram user, in ONE transaction:
  create or refresh the member, set them active, copy their spreadsheet
  answers into kyc_responses, mark the row claimed, write the audit trail.

  Returns False, changing nothing, if anything shifted underneath us."""
  with get_conn() as conn:
    row = conn.execute(
      "SELECT linked_user_id FROM legacy_members WHERE legacy_id = ?",
      (legacy_id,),
    ).fetchone()
    if row is None or row["linked_user_id"] is not None:
      return False
    if conn.execute(
        "SELECT 1 FROM legacy_members WHERE linked_user_id = ?", (user_id,)
    ).fetchone():
      return False
    member = conn.execute(
      "SELECT status FROM members WHERE user_id = ?", (user_id,)
    ).fetchone()
    if member is not None and member["status"] not in _LEGACY_PROMOTABLE:
      return False

    upsert_active(conn, user_id, username, first_name, last_name)
    # Answers they've already given the bot win over the old spreadsheet.
    conn.execute(
      """
      INSERT OR IGNORE INTO kyc_responses
        (user_id, field_key, value_text, needs_review)
      SELECT ?, field_key, value_text, needs_review
      FROM legacy_responses WHERE legacy_id = ?
      """,
      (user_id, legacy_id),
    )
    conn.execute(
      "UPDATE legacy_members SET linked_user_id = ?, linked_at = datetime('now') "
      "WHERE legacy_id = ?",
      (user_id, legacy_id),
    )
    if member is None or member["status"] != STATUS_ACTIVE:
      conn.execute(
        "INSERT INTO member_events (user_id, event, note) VALUES (?, ?, ?)",
        (user_id, f"status:{STATUS_ACTIVE}", f"legacy link via {method}"),
      )
    conn.execute(
      "INSERT INTO member_events (user_id, event, note) VALUES (?, ?, ?)",
      (user_id, "legacy_linked", f"legacy_id={legacy_id}; via {method}"),
    )
  return True


def activate_main_group_member(user_id, username, first_name, last_name, method):
  """Make a verified main-group member active when there's no spreadsheet
  record to link. Being in the main group is proof of membership; their
  profile simply starts empty. Returns False, changing nothing, if their
  status is one a human should decide (under review, declined, removed)."""
  with get_conn() as conn:
    member = conn.execute(
      "SELECT status FROM members WHERE user_id = ?", (user_id,)
    ).fetchone()
    if member is not None and member["status"] not in _LEGACY_PROMOTABLE:
      return False
    upsert_active(conn, user_id, username, first_name, last_name)
    if member is None or member["status"] != STATUS_ACTIVE:
      conn.execute(
        "INSERT INTO member_events (user_id, event, note) VALUES (?, ?, ?)",
        (user_id, f"status:{STATUS_ACTIVE}",
         f"main-group member, no old record (via {method})"),
      )
  return True
