"""The weekly brief's tables (src/brief/).

Times are UTC and stored the way datetime('now') writes them,
'YYYY-MM-DD HH:MM:SS', so plain string comparison orders them correctly.
"""
import json

from src.db.connection import get_conn
from src.db.schema import STATUS_ACTIVE
from src.kyc_form.fields import BRIEF_MENTIONS_KEY, BRIEF_MENTIONS_YES
# ===============================================================

# A UTC day as a half-open window: from 'YYYY-MM-DD' up to the next day.
_IN_DAY = "sent_at >= ? AND sent_at < date(?, '+1 day')"


# ----- Groups ---------------------------------------------------------
def ensure_brief_group(chat_id, enabled):
  """Record a group if it isn't recorded yet. Never changes an existing row."""
  with get_conn() as conn:
    conn.execute(
      "INSERT OR IGNORE INTO brief_groups (chat_id, enabled) VALUES (?, ?)",
      (chat_id, int(enabled)),
    )


def set_brief_group(chat_id, enabled, added_by):
  """Alpha was just added to a group: record who added it and whether it counts."""
  with get_conn() as conn:
    conn.execute(
      """
      INSERT INTO brief_groups (chat_id, enabled, added_by) VALUES (?, ?, ?)
      ON CONFLICT(chat_id) DO UPDATE SET
        enabled  = excluded.enabled,
        added_by = excluded.added_by,
        added_at = datetime('now')
      """,
      (chat_id, int(enabled), added_by),
    )


def remove_brief_group(chat_id):
  """Alpha left a group: forget it, and its messages not yet digested."""
  with get_conn() as conn:
    conn.execute("DELETE FROM brief_groups WHERE chat_id = ?", (chat_id,))
    conn.execute("DELETE FROM brief_messages WHERE chat_id = ?", (chat_id,))


def brief_group_enabled(chat_id):
  with get_conn() as conn:
    row = conn.execute(
      "SELECT enabled FROM brief_groups WHERE chat_id = ?", (chat_id,)
    ).fetchone()
  return bool(row and row["enabled"])


# ----- Raw messages ---------------------------------------------------
def save_brief_message(chat_id, message_id, user_id, reply_to_user_id, text, sent_at):
  """IGNORE: Telegram can deliver the same update twice after a restart."""
  with get_conn() as conn:
    conn.execute(
      "INSERT OR IGNORE INTO brief_messages "
      "(chat_id, message_id, user_id, reply_to_user_id, text, sent_at) "
      "VALUES (?, ?, ?, ?, ?, ?)",
      (chat_id, message_id, user_id, reply_to_user_id, text, sent_at),
    )


def finished_brief_days(limit=50):
  """(chat_id, day) pairs that still have raw messages from a day that has
  ended. date('now') is today's UTC midnight, so today is never included."""
  with get_conn() as conn:
    return conn.execute(
      "SELECT chat_id, date(sent_at) AS day FROM brief_messages "
      "WHERE sent_at < date('now') "
      "GROUP BY chat_id, day ORDER BY day LIMIT ?",
      (limit,),
    ).fetchall()


def brief_messages_for_day(chat_id, day):
  with get_conn() as conn:
    return conn.execute(
      f"SELECT id, user_id, reply_to_user_id, text FROM brief_messages "
      f"WHERE chat_id = ? AND {_IN_DAY} ORDER BY sent_at, message_id",
      (chat_id, day, day),
    ).fetchall()


# ----- Daily digests --------------------------------------------------
def brief_day_exists(chat_id, day):
  with get_conn() as conn:
    return conn.execute(
      "SELECT 1 FROM brief_days WHERE day = ? AND chat_id = ?", (day, chat_id)
    ).fetchone() is not None


def save_brief_day(chat_id, day, message_count, replies, digest, up_to_id):
  """Store a day's counts and digest AND delete its raw messages, in one
  transaction: both happen or neither does.

  A row that already exists (a late message for a day already digested) has
  the new counts added to it and keeps its digest. Only raw messages up to
  up_to_id are deleted: anything that arrived after they were read waits
  for the next run."""
  with get_conn() as conn:
    row = conn.execute(
      "SELECT message_count, replies_json FROM brief_days "
      "WHERE day = ? AND chat_id = ?",
      (day, chat_id),
    ).fetchone()
    if row is None:
      conn.execute(
        "INSERT INTO brief_days "
        "(day, chat_id, message_count, replies_json, digest_json) "
        "VALUES (?, ?, ?, ?, ?)",
        (day, chat_id, message_count, json.dumps(replies),
         json.dumps(digest) if digest is not None else None),
      )
    else:
      merged = {int(k): v for k, v in json.loads(row["replies_json"]).items()}
      for user_id, n in replies.items():
        merged[user_id] = merged.get(user_id, 0) + n
      conn.execute(
        "UPDATE brief_days SET message_count = ?, replies_json = ? "
        "WHERE day = ? AND chat_id = ?",
        (row["message_count"] + message_count, json.dumps(merged), day, chat_id),
      )
    conn.execute(
      f"DELETE FROM brief_messages WHERE chat_id = ? AND {_IN_DAY} AND id <= ?",
      (chat_id, day, day, up_to_id),
    )


def brief_week_days(week_start):
  """Every group's rows for the 7 days starting on week_start."""
  with get_conn() as conn:
    return conn.execute(
      "SELECT * FROM brief_days "
      "WHERE day >= ? AND day < date(?, '+7 days') ORDER BY day",
      (week_start, week_start),
    ).fetchall()


def purge_brief_data(keep_days):
  """Safety net: whatever happened, nothing outlives keep_days."""
  cutoff = f"-{int(keep_days)} days"
  with get_conn() as conn:
    conn.execute(
      "DELETE FROM brief_messages WHERE sent_at < datetime('now', ?)", (cutoff,))
    conn.execute(
      "DELETE FROM brief_days WHERE day < date('now', ?)", (cutoff,))


# ----- Weekly brief ---------------------------------------------------
def claim_brief_week(week_start):
  """True only for the one run that gets to send this week's brief."""
  with get_conn() as conn:
    cur = conn.execute(
      "INSERT OR IGNORE INTO brief_weeks (week_start, status) VALUES (?, 'sending')",
      (week_start,),
    )
    return cur.rowcount == 1


def release_brief_week(week_start):
  """Sending failed: let the next run try again."""
  with get_conn() as conn:
    conn.execute(
      "DELETE FROM brief_weeks WHERE week_start = ? AND status = 'sending'",
      (week_start,),
    )


def finish_brief_week(week_start, message_id, used_llm):
  """Mark the brief sent AND delete the week's digests, in one transaction."""
  with get_conn() as conn:
    conn.execute(
      "UPDATE brief_weeks SET status = 'sent', message_id = ?, used_llm = ?, "
      "sent_at = datetime('now') WHERE week_start = ?",
      (message_id, int(used_llm), week_start),
    )
    conn.execute(
      "DELETE FROM brief_days WHERE day < date(?, '+7 days')", (week_start,))


# ----- Numbers for the brief ------------------------------------------
def brief_event_counts(week_start):
  """{event: count} for the week, plus 'profiles_updated': members (not
  edits) who saved or changed profile details."""
  with get_conn() as conn:
    counts = dict(conn.execute(
      "SELECT event, COUNT(*) FROM member_events "
      "WHERE created_at >= datetime(?) AND created_at < datetime(?, '+7 days') "
      "GROUP BY event",
      (week_start, week_start),
    ).fetchall())
    counts["profiles_updated"] = conn.execute(
      "SELECT COUNT(DISTINCT user_id) FROM member_events "
      "WHERE created_at >= datetime(?) AND created_at < datetime(?, '+7 days') "
      "AND event IN ('profile_updated', 'profile_edited')",
      (week_start, week_start),
    ).fetchone()[0]
  return counts


def count_active_members():
  with get_conn() as conn:
    return conn.execute(
      "SELECT COUNT(*) FROM members WHERE status = ?", (STATUS_ACTIVE,)
    ).fetchone()[0]


def mentionable_members(user_ids):
  """{user_id: row} for the active members among user_ids who said Yes to
  being mentioned."""
  if not user_ids:
    return {}
  marks = ", ".join("?" for _ in user_ids)
  with get_conn() as conn:
    rows = conn.execute(
      f"""
      SELECT m.user_id, m.username, m.first_name
      FROM members m
      JOIN kyc_responses ok ON ok.user_id = m.user_id
       AND ok.field_key = ? AND ok.value_text = ?
      WHERE m.status = ? AND m.user_id IN ({marks})
      """,
      (BRIEF_MENTIONS_KEY, BRIEF_MENTIONS_YES, STATUS_ACTIVE, *user_ids),
    ).fetchall()
  return {r["user_id"]: r for r in rows}


def mentionable_birthdays(mm_dd_list):
  """Active, opted-in members whose birthday (stored as MM-DD) is in the list."""
  if not mm_dd_list:
    return []
  marks = ", ".join("?" for _ in mm_dd_list)
  with get_conn() as conn:
    return conn.execute(
      f"""
      SELECT m.user_id, m.username, m.first_name, b.value_text AS birthday
      FROM members m
      JOIN kyc_responses ok ON ok.user_id = m.user_id
       AND ok.field_key = ? AND ok.value_text = ?
      JOIN kyc_responses b ON b.user_id = m.user_id
       AND b.field_key = 'birthday'
      WHERE m.status = ? AND b.value_text IN ({marks})
      """,
      (BRIEF_MENTIONS_KEY, BRIEF_MENTIONS_YES, STATUS_ACTIVE, *mm_dd_list),
    ).fetchall()
