import logging

from telegram.constants import ChatMemberStatus
from telegram.error import TelegramError

import db
from config import LEADERSHIP_GROUP_ID
# =========================================================


logger = logging.getLogger(__name__)

MODULE = "leadership"

# Statuses that mean "currently in the group"
ACTIVE_STATUSES = {
  ChatMemberStatus.OWNER,
  ChatMemberStatus.ADMINISTRATOR,
  ChatMemberStatus.MEMBER,
}

# ------------------------------------------------------
async def is_in_leadership_group(bot, user_id) -> bool:
  try:
    member = await bot.get_chat_member(LEADERSHIP_GROUP_ID, user_id)
  except TelegramError as exc:
    logger.warning("Membership check failed for %s: %s", user_id, exc)
    return False
  return member.status in ACTIVE_STATUSES


async def handle_registration(update, context):
  user = update.effective_user
  db.upsert_member(user.id, user.full_name, user.username)

  if not await is_in_leadership_group(context.bot, user.id):
    await update.message.reply_text(
      "Sorry — I can't sign you up for leadership meeting reminders, "
      "as you're not currently in the ATL leadership team."
    )
    return

  db.register_module(user.id, MODULE)
  await update.message.reply_text(
    "✅ You're registered for ATL leadership meeting reminders."
  )