"""Step 2 of the weekly brief: once a UTC day is over, turn each group's
messages from that day into counts and a short Gemini digest, then delete
the messages.

main.py runs digest_finished_days every hour, but it only touches days that
have ended, so in practice each day is digested just after midnight UTC. If
the bot was down, it catches up on its next run. Saving the digest and
deleting the messages happen in one transaction (db.save_brief_day).

If Gemini fails, the messages are kept and the day is tried again next run.
After BRIEF_DIGEST_GIVE_UP_DAYS the day is saved without a digest (its
counts still go in the brief) and the messages are deleted anyway.
"""
import asyncio
import logging
from collections import Counter
from datetime import date, datetime, timezone

from src import db
from src.assistant.llm import digest_day
from src.config import (BRIEF_DIGEST_GIVE_UP_DAYS, BRIEF_DIGEST_MAX_CHARS,
                        BRIEF_KEEP_DAYS)
# ===========================================================================
logger = logging.getLogger(__name__)

# The hourly job and Monday's brief both call this. The lock stops them
# digesting the same day at the same time and counting it twice.
_lock = asyncio.Lock()


def _as_text(rows):
  """The day's messages for Gemini: text only, no names or ids. If there's
  too much, the newest messages are kept."""
  lines, size = [], 0
  for row in reversed(rows):
    line = "- " + " ".join(row["text"].split())
    size += len(line) + 1
    if size > BRIEF_DIGEST_MAX_CHARS:
      break
    lines.append(line)
  return "\n".join(reversed(lines))


async def digest_finished_days(context=None):
  async with _lock:
    today = datetime.now(timezone.utc).date()
    for row in db.finished_brief_days():
      chat_id, day = row["chat_id"], row["day"]
      messages = db.brief_messages_for_day(chat_id, day)
      if not messages:
        continue
      replies = Counter(m["reply_to_user_id"] for m in messages
                        if m["reply_to_user_id"])

      digest = None
      if not db.brief_day_exists(chat_id, day):   # late messages: counts only
        digest = await digest_day(_as_text(messages))
        too_old = (today - date.fromisoformat(day)).days > BRIEF_DIGEST_GIVE_UP_DAYS
        if digest is None and not too_old:
          logger.warning("Brief: digest for chat %s on %s failed, will retry",
                         chat_id, day)
          continue

      senders = {m["user_id"] for m in messages}
      db.save_brief_day(chat_id, day, len(messages), dict(replies), senders,
                        digest, up_to_id=max(m["id"] for m in messages))
      logger.info("Brief: digested chat %s on %s (%d messages%s)", chat_id, day,
                  len(messages), "" if digest else ", no digest")

    db.purge_brief_data(BRIEF_KEEP_DAYS)
