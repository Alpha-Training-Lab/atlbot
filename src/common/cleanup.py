"""The repeating job that deletes messages once their time is up.

Any feature can call db.schedule_deletion(chat_id, message_id, seconds)
instead of deleting straight away; main.py runs sweep_deletions every
5 minutes."""
import logging

from telegram.ext import ContextTypes

from src import db
# ===========================================================================
logger = logging.getLogger(__name__)


async def sweep_deletions(context: ContextTypes.DEFAULT_TYPE):
  rows = db.due_deletions()
  if not rows:
    return
  logger.info("Sweeping %d scheduled deletion(s)", len(rows))
  for row in rows:
    try:
      await context.bot.delete_message(row["chat_id"], row["message_id"])
    except Exception as e:
      logger.warning("Could not delete %s in %s: %s",
                     row["message_id"], row["chat_id"], e)
    db.clear_deletion(row["id"])   # clear either way — don't retry forever
