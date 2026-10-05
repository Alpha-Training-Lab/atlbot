"""Steps 3 and 4 of the weekly brief: every Monday from 09:00 UTC, build the
brief for the week that just ended (Monday to Sunday) and post it in the
induction group.

Two parts:
  - numbers straight from SQLite: nothing an LLM can get wrong;
  - highlights, lessons and celebrations Gemini merged from the daily
    digests, used ONLY if they pass two checks: a pattern check in code,
    then a Gemini compliance check.
If anything about the Gemini part fails, the brief goes out with the
numbers only. Nobody approves it and nobody is sent a copy.

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
from src.brief.digest import digest_finished_days
from src.config import (BRIEF_HOUR_UTC, BRIEF_LAST_HOUR_UTC, BRIEF_MILESTONES,
                        INDUCTION_GROUP_ID)
# ===========================================================================
logger = logging.getLogger(__name__)

# Telegram's limit is 4,096 characters; emoji can count double, so keep a margin.
MAX_LENGTH = 3800

# Anything that looks like a signal, an instruction to trade or a forecast.
# Deliberately strict: a false alarm only means a numbers-only brief.
_SIGNAL_PATTERNS = re.compile("|".join([
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


# ----- Numbers ---------------------------------------------------------
def _name(row):
  """@username, else first name. Never a last name."""
  if row["username"]:
    return f"@{row['username']}"
  return row["first_name"] or "a member"


def _day_label(d):
  return f"{d.day} {d.strftime('%b')}"


def _coming_week(week_start):
  """The 7 days from this Monday: birthdays and milestones look ahead."""
  monday = date.fromisoformat(week_start) + timedelta(days=7)
  return [monday + timedelta(days=i) for i in range(7)]


def _is_leap(year):
  return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)


def _birthdays(days):
  """[(date, name)] for opted-in members with a birthday in days. A 29 Feb
  birthday is celebrated on 28 Feb in other years."""
  wanted = {d.strftime("%m-%d"): d for d in days}
  if "02-28" in wanted and not _is_leap(wanted["02-28"].year):
    wanted["02-29"] = wanted["02-28"]
  rows = db.mentionable_birthdays(list(wanted))
  return sorted((wanted[r["birthday"]], _name(r)) for r in rows)


def _milestones(days):
  found = []
  for name, founded in BRIEF_MILESTONES:
    start = date.fromisoformat(founded)
    for d in days:
      if (d.month, d.day) == (start.month, start.day) and d.year > start.year:
        found.append(f"{name} turns {d.year - start.year} on {_day_label(d)}")
  return found


def _helpers(day_rows, limit=3):
  """The opted-in members whose messages got the most replies."""
  replies = Counter()
  for row in day_rows:
    for user_id, n in json.loads(row["replies_json"]).items():
      replies[int(user_id)] += n
  candidates = [uid for uid, _ in replies.most_common(50)]
  allowed = db.mentionable_members(candidates)
  return [_name(allowed[uid]) for uid in candidates if uid in allowed][:limit]


async def _group_activity(bot, day_rows):
  per_group = Counter()
  for row in day_rows:
    per_group[row["chat_id"]] += row["message_count"]
  lines = []
  for chat_id, n in per_group.most_common():
    try:
      title = (await bot.get_chat(chat_id)).title or "A group"
    except Exception:
      title = "A group"
    lines.append(f"{title}: {n:,} messages")
  return lines


async def _gather(bot, week_start):
  day_rows = db.brief_week_days(week_start)
  coming = _coming_week(week_start)
  return {
    "week_start": week_start,
    "events": db.brief_event_counts(week_start),
    "active": db.count_active_members(),
    "birthdays": _birthdays(coming),
    "milestones": _milestones(coming),
    "helpers": _helpers(day_rows),
    "activity": await _group_activity(bot, day_rows),
    "day_rows": day_rows,
  }


# ----- Gemini's part ---------------------------------------------------
_SECTION_TITLES = {
  "highlights": "🌟 COMMUNITY HIGHLIGHTS",
  "lessons": "📚 LESSONS FROM THE WEEK",
  "celebrations": "🏆 WINS & CELEBRATIONS",
}


def _render_sections(sections):
  blocks = []
  for key, title in _SECTION_TITLES.items():
    items = sections.get(key) or []
    if items:
      blocks.append(title + "\n" + "\n".join(f"• {i}" for i in items))
  return "\n\n".join(blocks)


async def _llm_sections(day_rows):
  """Gemini's sections if they pass both checks, else None."""
  notes = [json.loads(r["digest_json"]) for r in day_rows if r["digest_json"]]
  if not notes:
    return None
  merged = await merge_week(json.dumps(notes, ensure_ascii=False))
  if not merged or not any(merged.values()):
    return None
  text = _render_sections(merged)
  if _SIGNAL_PATTERNS.search(text):
    logger.warning("Brief: Gemini's sections blocked by the pattern check")
    return None
  if not await brief_passes_check(text):
    logger.warning("Brief: Gemini's sections blocked by the compliance check")
    return None
  return merged


# ----- The message -----------------------------------------------------
# "Label: number" reads right for 1 or 1,000, so no plural rules needed.
_NUMBER_LINES = [
  ("joined_induction", "New in the induction group"),
  ("status:pending_review", "Asked to be onboarded"),
  ("status:awaiting_dm", "Approved out of induction"),
  ("status:pending_access", "Completed registration"),
  ("status:active", "New active members"),
  ("profiles_updated", "Members who updated their profile"),
]


def render(stats, sections=None):
  """The whole brief as plain text. Empty sections are left out."""
  start = date.fromisoformat(stats["week_start"])
  end = start + timedelta(days=6)
  events = stats["events"]
  blocks = [
    "📊 ATL WEEKLY BRIEF\n"
    f"{start:%a} {_day_label(start)} – {end:%a} {_day_label(end)} {end:%Y}"
  ]

  numbers = [f"• {label}: {events.get(k, 0):,}"
             for k, label in _NUMBER_LINES if events.get(k, 0)]
  numbers.append(f"• Active members in total: {stats['active']:,}")
  blocks.append("📈 WEEK IN NUMBERS\n" + "\n".join(numbers))

  if sections:
    blocks.append(_render_sections(sections))

  if stats["birthdays"]:
    names = " · ".join(f"{name} ({_day_label(d)})" for d, name in stats["birthdays"])
    blocks.append(f"🎂 BIRTHDAYS THIS WEEK\n{names}\n"
                  "Happy birthday from the whole ATL family!")

  if stats["milestones"]:
    blocks.append("🎉 MILESTONES\n" + "\n".join(f"• {m}" for m in stats["milestones"]))

  if stats["helpers"]:
    ranked = "\n".join(f"{i}. {n}" for i, n in enumerate(stats["helpers"], 1))
    blocks.append("🤝 MOST HELPFUL THIS WEEK\n"
                  f"Members whose messages got the most replies:\n{ranked}")

  if stats["activity"]:
    blocks.append("💬 COMMUNITY ACTIVITY\n"
                  + "\n".join(f"• {line}" for line in stats["activity"]))

  moved_on = events.get("status:awaiting_dm", 0)
  if moved_on:
    blocks.append("🚪 STILL IN INDUCTION?\n"
                  f"Moved on from induction this week: {moved_on:,}. "
                  "Finished the induction material? Follow the pinned message "
                  "to take your next step.")

  blocks.append("🛡️ SAFETY REMINDER\n"
                "Official payment handlers never DM you first. "
                "If someone does, report them to an admin.")
  blocks.append("Education only. Not financial advice.\n"
                "Alpha Training Lab: Your Leverage to the better life you seek.")
  return "\n\n".join(blocks)


async def build_brief(bot, week_start):
  """(text, used_llm). Falls back to numbers only whenever it has to."""
  stats = await _gather(bot, week_start)
  sections = await _llm_sections(stats["day_rows"])
  if sections:
    text = render(stats, sections)
    if len(text) <= MAX_LENGTH:
      return text, True
    logger.warning("Brief: too long with Gemini's sections, sending numbers only")
  return render(stats)[:MAX_LENGTH], False


# ----- The job ---------------------------------------------------------
async def publish_weekly_brief(context: ContextTypes.DEFAULT_TYPE):
  week_start = _due_week(datetime.now(timezone.utc))
  if week_start is None or not INDUCTION_GROUP_ID:
    return
  if not db.claim_brief_week(week_start):
    return   # this week's brief is already sent (or being sent)

  try:
    await digest_finished_days(context)   # make sure Sunday is in
    text, used_llm = await build_brief(context.bot, week_start)
    sent = await context.bot.send_message(
      INDUCTION_GROUP_ID, text,
      link_preview_options=LinkPreviewOptions(is_disabled=True),
    )
  except Exception:
    db.release_brief_week(week_start)   # the next run tries again
    logger.exception("Brief: sending the week of %s failed, will retry", week_start)
    return

  db.finish_brief_week(week_start, sent.message_id, used_llm)
  logger.info("Brief: sent the week of %s (%s)", week_start,
              "with Gemini's highlights" if used_llm else "numbers only")
