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


def save_brief_day(chat_id, day, message_count, replies, senders, digest, up_to_id):
  """Store a day's counts and digest AND delete its raw messages, in one
  transaction: both happen or neither does.

  replies: {user_id: replies their messages got}. senders: who posted.
  A row that already exists (a late message for a day already digested) has
  the new counts added to it and keeps its digest. Only raw messages up to
  up_to_id are deleted: anything that arrived after they were read waits
  for the next run."""
  with get_conn() as conn:
    row = conn.execute(
      "SELECT message_count, replies_json, senders_json FROM brief_days "
      "WHERE day = ? AND chat_id = ?",
      (day, chat_id),
    ).fetchone()
    if row is None:
      conn.execute(
        "INSERT INTO brief_days "
        "(day, chat_id, message_count, replies_json, senders_json, digest_json) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (day, chat_id, message_count, json.dumps(replies),
         json.dumps(sorted(senders)),
         json.dumps(digest) if digest is not None else None),
      )
    else:
      merged = {int(k): v for k, v in json.loads(row["replies_json"]).items()}
      for user_id, n in replies.items():
        merged[user_id] = merged.get(user_id, 0) + n
      everyone = set(json.loads(row["senders_json"])) | set(senders)
      conn.execute(
        "UPDATE brief_days SET message_count = ?, replies_json = ?, "
        "senders_json = ? WHERE day = ? AND chat_id = ?",
        (row["message_count"] + message_count, json.dumps(merged),
         json.dumps(sorted(everyone)), day, chat_id),
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
    conn.execute(
      "DELETE FROM brief_birthdays WHERE day < date('now', ?)", (cutoff,))


# ----- Birthdays (felicitation group) ---------------------------------
def save_birthdays(day, celebrants):
  """IGNORE: the same person wished twice on one day is one birthday."""
  with get_conn() as conn:
    conn.executemany(
      "INSERT OR IGNORE INTO brief_birthdays (day, celebrant) VALUES (?, ?)",
      [(day, c) for c in celebrants],
    )


def week_celebrants(week_start):
  """Everyone wished a happy birthday during the week, once each."""
  with get_conn() as conn:
    rows = conn.execute(
      "SELECT DISTINCT celebrant FROM brief_birthdays "
      "WHERE day >= ? AND day < date(?, '+7 days')",
      (week_start, week_start),
    ).fetchall()
  return [r["celebrant"] for r in rows]


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
    conn.execute(
      "DELETE FROM brief_birthdays WHERE day < date(?, '+7 days')", (week_start,))


# ----- Numbers for the brief ------------------------------------------
# A member is "new" when an admin approved their registration or the owner
# added them. Existing members being recognised (legacy links, main-group
# members with no old record) are also logged as status:active, so their
# notes are excluded here.
_NEW_MEMBER = ("event = 'status:active' AND (note IS NULL "
               "OR note = 'added to the main group by the owner')")


def brief_growth_counts(week_start):
  """{'new_members': n, 'new_in_induction': n} for the week."""
  window = ("created_at >= datetime(?) AND created_at < datetime(?, '+7 days')")
  with get_conn() as conn:
    def count(where):
      return conn.execute(
        f"SELECT COUNT(DISTINCT user_id) FROM member_events "
        f"WHERE {window} AND {where}",
        (week_start, week_start),
      ).fetchone()[0]
    return {
      "new_members": count(_NEW_MEMBER),
      "new_in_induction": count("event = 'joined_induction'"),
    }


# ----- Who may be named -----------------------------------------------
_OPTED_IN = """
  SELECT m.user_id, m.username, m.first_name
  FROM members m
  JOIN kyc_responses ok ON ok.user_id = m.user_id
   AND ok.field_key = ? AND ok.value_text = ?
  WHERE m.status = ?
"""


def mentionable_members(user_ids):
  """{user_id: row} for the active members among user_ids who said Yes to
  being mentioned."""
  if not user_ids:
    return {}
  marks = ", ".join("?" for _ in user_ids)
  with get_conn() as conn:
    rows = conn.execute(
      _OPTED_IN + f" AND m.user_id IN ({marks})",
      (BRIEF_MENTIONS_KEY, BRIEF_MENTIONS_YES, STATUS_ACTIVE, *user_ids),
    ).fetchall()
  return {r["user_id"]: r for r in rows}


def user_ids_for_usernames(usernames):
  """{lowercase username: user_id} for anyone on record, opted in or not.
  Used to count a person once, whether wished by @username or by name."""
  if not usernames:
    return {}
  marks = ", ".join("?" for _ in usernames)
  with get_conn() as conn:
    rows = conn.execute(
      f"SELECT user_id, lower(username) AS username FROM members "
      f"WHERE lower(username) IN ({marks})",
      tuple(usernames),
    ).fetchall()
  return {r["username"]: r["user_id"] for r in rows}
