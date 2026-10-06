"""Steps 3 and 4 of the weekly brief: every Monday from 09:00 UTC, build the
brief for the week that just ended (Monday to Sunday) and post it in the
induction group.

Who it's for: people still in induction. It gives them a peek at life
inside ATL, then shows them how to get in.

Two parts:
  - numbers and names straight from SQLite: nothing an LLM can get wrong;
  - the week's summary, lesson, wins and what's coming up, which Gemini
    writes from the daily digests. Used ONLY if they pass two checks: a
    pattern check in code, then a Gemini compliance check.
If the Gemini part fails, the brief still goes out without it. Nobody
approves it and nobody is sent a copy.

main.py runs publish_weekly_brief every 10 minutes. It does nothing except
on Monday between BRIEF_HOUR_UTC and BRIEF_LAST_HOUR_UTC, and brief_weeks
makes sure each week's brief is sent once.
"""
import json
import logging
import re
from collections import Counter
from datetime import date, datetime, timedelta, timezone

from telegram import LinkPreviewOptions
from telegram.ext import ContextTypes

from src import db
from src.assistant.llm import brief_passes_check, merge_week
from src.brief.content import SAFETY_TIPS, pick_for_week
from src.brief.digest import digest_finished_days
from src.config import (BRIEF_HIDE_BELOW, BRIEF_HOUR_UTC, BRIEF_LAST_HOUR_UTC,
                        BRIEF_MILESTONES, INDUCTION_GROUP_ID, INDUCTION_PINNED_URL)
# ===========================================================================
logger = logging.getLogger(__name__)

# Telegram's limit is 4,096 characters; emoji can count double, so keep a margin.
MAX_LENGTH = 3800
MAX_BIRTHDAY_NAMES = 10

# Anything that looks like a signal, an instruction to trade, a forecast or
# a handle. Deliberately strict: a false alarm only drops Gemini's part.
_RISKY = re.compile("|".join([
  r"\b(entry|entries|tp\d?|take[\s-]?profit|sl|stop[\s-]?loss|target)\b\W{0,3}\$?\d",
  r"\b(buy|sell|go long|go short|longing|shorting)\b",
  r"\bwill (hit|reach|pump|dump|moon|crash|rise|fall|drop)\b",
  r"\b(heading (to|for)|price target|to the moon|guaranteed|risk[\s-]?free)\b",
  r"\b\d+\s?x\s+(returns?|profits?|gains?)\b",
  r"\bsignals?\b",
  r"@\w",
]), re.IGNORECASE)


# ----- When ------------------------------------------------------------
def _due_week(now):
  """The Monday ('YYYY-MM-DD') starting the week whose brief is due, or None.
  Python's weekday() counts Monday as 0."""
  if now.weekday() != 0:
    return None
  if not BRIEF_HOUR_UTC <= now.hour < BRIEF_LAST_HOUR_UTC:
    return None
  return (now.date() - timedelta(days=7)).isoformat()


# ----- Helpers ---------------------------------------------------------
def _name(row):
  """@username, else first name. Never a last name."""
  if row["username"]:
    return f"@{row['username']}"
  return row["first_name"] or "a member"


def _day_label(d):
  return f"{d.day} {d.strftime('%b')}"


def _and_join(items):
  return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


# ----- Week in numbers -------------------------------------------------
def _numbers(week_start, day_rows):
  """(shown, hidden): lines for WEEK IN NUMBERS. A count below
  BRIEF_HIDE_BELOW is hidden so a quiet week never looks small. No totals:
  only this week's activity."""
  growth = db.brief_growth_counts(week_start)
  senders, per_day = set(), Counter()
  replies = 0
  for row in day_rows:
    senders.update(json.loads(row["senders_json"]))
    per_day[row["day"]] += row["message_count"]
    replies += sum(json.loads(row["replies_json"]).values())
  messages = sum(per_day.values())

  counts = [
    ("New members welcomed into the main group", growth["new_members"]),
    ("Messages shared in the community", messages),
    ("Members who joined the conversations", len(senders)),
    ("Replies between members", replies),
    ("New faces here in induction", growth["new_in_induction"]),
  ]
  shown = [f"• {label}: {n:,}" for label, n in counts if n and n >= BRIEF_HIDE_BELOW]
  hidden = [f"{label}: {n}" for label, n in counts if not (n and n >= BRIEF_HIDE_BELOW)]
  if messages and messages >= BRIEF_HIDE_BELOW:
    busiest = max(per_day, key=per_day.get)
    shown.append(f"• Busiest day: {date.fromisoformat(busiest):%A}")
  return shown, hidden


# ----- Names -----------------------------------------------------------
def _birthdays(week_start):
  """(how many people were celebrated, names of those who opted in)."""
  celebrants = db.week_celebrants(week_start)
  usernames = [c[2:] for c in celebrants if c.startswith("u:")]
  known = db.user_ids_for_usernames(usernames)
  people = set()   # each person once, by Telegram id where we know it
  for c in celebrants:
    if c.startswith("id:"):
      people.add(int(c[3:]))
    else:
      people.add(known.get(c[2:], c))
  ids = [p for p in people if isinstance(p, int)]
  allowed = db.mentionable_members(ids)
  names = sorted(_name(allowed[i]) for i in ids if i in allowed)
  return len(people), names[:MAX_BIRTHDAY_NAMES]


def _helpers(day_rows, limit=3):
  """The opted-in members whose messages got the most replies."""
  replies = Counter()
  for row in day_rows:
    for user_id, n in json.loads(row["replies_json"]).items():
      replies[int(user_id)] += n
  candidates = [uid for uid, _ in replies.most_common(50)]
  allowed = db.mentionable_members(candidates)
  return [_name(allowed[uid]) for uid in candidates if uid in allowed][:limit]


def _milestones(week_start):
  """Anniversaries in the coming week (this Monday to Sunday)."""
  monday, _ = _coming_week(week_start)
  found = []
  for name, founded in BRIEF_MILESTONES:
    start = date.fromisoformat(founded)
    for d in (monday + timedelta(days=i) for i in range(7)):
      if (d.month, d.day) == (start.month, start.day) and d.year > start.year:
        found.append(f"{name} turns {d.year - start.year} on {_day_label(d)}")
  return found


# ----- Gemini's part ---------------------------------------------------
def _coming_week(week_start):
  """This Monday to Sunday: the week ahead of the readers."""
  monday = date.fromisoformat(week_start) + timedelta(days=7)
  return monday, monday + timedelta(days=6)


def _split_notes(day_rows, week_start):
  """(daily notes without events, events dated in the coming week). Each
  event keeps the day it was announced, so a changed date can be settled."""
  first, last = _coming_week(week_start)
  notes, candidates = [], []
  for row in day_rows:
    if not row["digest_json"]:
      continue
    day_notes = json.loads(row["digest_json"])
    for event in day_notes.pop("upcoming", []):
      if first <= date.fromisoformat(event["date"]) <= last:
        candidates.append({**event, "announced_on": row["day"]})
    notes.append(day_notes)
  return notes, candidates


async def _gemini_parts(day_rows, week_start):
  """{summary, lesson, celebrations, coming_up} if they pass both checks,
  else None."""
  notes, candidates = _split_notes(day_rows, week_start)
  if not notes:
    return None
  parts = await merge_week(json.dumps(
    {"daily_notes": notes, "upcoming_candidates": candidates}, ensure_ascii=False))
  if not parts:
    return None
  # Events only on dates that were really announced for the coming week.
  allowed = {c["date"] for c in candidates}
  parts["coming_up"] = sorted((e for e in parts["coming_up"] if e["date"] in allowed),
                              key=lambda e: e["date"])
  if not (parts["summary"] or parts["lesson"] or parts["celebrations"]
          or parts["coming_up"]):
    return None
  text = "\n".join([parts["summary"], parts["lesson"], *parts["celebrations"],
                    *(e["what"] for e in parts["coming_up"])])
  if _RISKY.search(text):
    logger.warning("Brief: Gemini's part blocked by the pattern check")
    return None
  if not await brief_passes_check(text):
    logger.warning("Brief: Gemini's part blocked by the compliance check")
    return None
  return parts


# ----- The message -----------------------------------------------------
async def gather(week_start, bot_username=None):
  """Everything the brief needs, Gemini's part included (or None)."""
  day_rows = db.brief_week_days(week_start)
  shown, hidden = _numbers(week_start, day_rows)
  birthday_count, birthday_names = _birthdays(week_start)
  return {
    "week_start": week_start,
    "bot_username": bot_username,
    "numbers": shown,
    "hidden_numbers": hidden,
    "birthday_count": birthday_count,
    "birthday_names": birthday_names,
    "helpers": _helpers(day_rows),
    "milestones": _milestones(week_start),
    "gemini": await _gemini_parts(day_rows, week_start),
  }


def _how_to_join(bot_username):
  """The same steps the induction welcome gives. The admins to tag are left
  out on purpose: finding them is part of reading the material."""
  read = (f"Read the induction material from the top: {INDUCTION_PINNED_URL}"
          if INDUCTION_PINNED_URL else
          "Read the induction material from the top (see the pinned message).")
  ask = f"@{bot_username}" if bot_username else "Alpha"
  return ("🚪 WANT TO BE PART OF THIS?\n"
          "Membership is free and permanent. To join the main group:\n"
          f"1. {read}\n"
          "2. When you finish, follow the instructions at the end carefully.\n"
          "3. An admin reviews you, then Alpha takes you through registration privately.\n"
          f"Questions while you read? Message {ask}.")


def render(stats, use_gemini=True):
  """The whole brief as plain text, in reading order for someone in
  induction: what life inside looks like, then how to get in. Empty
  sections are left out."""
  week_start = stats["week_start"]
  start = date.fromisoformat(week_start)
  end = start + timedelta(days=6)
  gemini = stats["gemini"] if use_gemini else None
  blocks = [
    "👀 A PEEK INSIDE ATL\n"
    f"What happened in the community, {start:%a} {_day_label(start)} – "
    f"{end:%a} {_day_label(end)}"
  ]

  if gemini and gemini["summary"]:
    blocks.append("🗣️ THIS WEEK INSIDE ATL\n" + gemini["summary"])

  if stats["numbers"]:
    blocks.append("📈 THE WEEK IN NUMBERS\n" + "\n".join(stats["numbers"]))

  if gemini and gemini["lesson"]:
    blocks.append("📚 LESSON OF THE WEEK\n" + gemini["lesson"])

  if gemini and gemini["celebrations"]:
    blocks.append("🏆 WINS & CELEBRATIONS\n"
                  + "\n".join(f"• {c}" for c in gemini["celebrations"]))

  n = stats["birthday_count"]
  if n:
    line = f"This week we celebrated {n} {'birthday' if n == 1 else 'birthdays'}"
    if stats["birthday_names"]:
      line += ", including " + _and_join(stats["birthday_names"])
    blocks.append(f"🎂 BIRTHDAYS\n{line}. Happy birthday from the whole ATL family!")

  if stats["helpers"]:
    ranked = "\n".join(f"{i}. {name}" for i, name in enumerate(stats["helpers"], 1))
    blocks.append("🤝 MEMBERS WHO HELPED OTHERS\n"
                  f"Their messages got the most replies this week:\n{ranked}")

  if stats["milestones"]:
    blocks.append("🎉 MILESTONES\n" + "\n".join(f"• {m}" for m in stats["milestones"]))

  if gemini and gemini["coming_up"]:
    lines = []
    for event in gemini["coming_up"]:
      d = date.fromisoformat(event["date"])
      lines.append(f"• {d:%a} {_day_label(d)}: {event['what']}")
    blocks.append("📅 COMING UP THIS WEEK\nInside the main group:\n" + "\n".join(lines))

  blocks.append(_how_to_join(stats["bot_username"]))
  blocks.append("🛡️ STAY SAFE\n" + pick_for_week(SAFETY_TIPS, week_start, salt="safety"))
  blocks.append("Education only. Not financial advice.\n"
                "Alpha Training Lab: Your Leverage to the better life you seek.")
  return "\n\n".join(blocks)


async def build_brief(bot, week_start):
  """(text, used_gemini) for the week."""
  return compose(await gather(week_start, bot_username=bot.username))


def compose(stats):
  """(text, used_gemini). Leaves Gemini's part out whenever it has to."""
  if stats["gemini"]:
    text = render(stats)
    if len(text) <= MAX_LENGTH:
      return text, True
    logger.warning("Brief: too long with Gemini's part, sending without it")
  return render(stats, use_gemini=False)[:MAX_LENGTH], False


# ----- The job ---------------------------------------------------------
async def publish_weekly_brief(context: ContextTypes.DEFAULT_TYPE):
  week_start = _due_week(datetime.now(timezone.utc))
  if week_start is None or not INDUCTION_GROUP_ID:
    return
  if not db.claim_brief_week(week_start):
    return   # this week's brief is already sent (or being sent)

  try:
    await digest_finished_days(context)   # make sure Sunday is in
    text, used_gemini = await build_brief(context.bot, week_start)
    sent = await context.bot.send_message(
      INDUCTION_GROUP_ID, text,
      link_preview_options=LinkPreviewOptions(is_disabled=True),
    )
  except Exception:
    db.release_brief_week(week_start)   # the next run tries again
    logger.exception("Brief: sending the week of %s failed, will retry", week_start)
    return

  db.finish_brief_week(week_start, sent.message_id, used_gemini)
  logger.info("Brief: sent the week of %s (%s)", week_start,
              "with Gemini's part" if used_gemini else "without Gemini's part")
