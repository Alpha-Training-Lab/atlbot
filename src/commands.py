"""Slash commands. /start routes a deep link to the feature it belongs to:

  ?start=kyc   registration (onboarding/kyc.py)
  ?start=link  "I'm already an ATL member" (members/legacy.py)
  no payload   an active member's profile, or a pointer to their group
"""
from telegram import Update
from telegram.ext import ContextTypes

from src import db
from src.members import legacy, profile
from src.onboarding import kyc
# ===========================================================================


async def _enter_kyc(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
  user = update.effective_user
  db.upsert_member(user.id, username=user.username,
                   first_name=user.first_name, last_name=user.last_name)
  await kyc.handle_kyc_entry(context.bot, user.id)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
  payload = context.args[0] if context.args else None

  if payload == "kyc":
    await _enter_kyc(update, context)
    return

  if payload == "link":
    await legacy.handle_link_start(update, context)
    return

  member = db.get_member(update.effective_user.id)
  if member is not None and member["status"] == db.STATUS_ACTIVE:
    await profile.show_profile(context.bot, update.effective_user.id)
    return

  await update.message.reply_text(
    "👋 Hi! I'm Alpha, an ATL task automation bot. Please use the link "
    "shared in your ATL group so I know what to sign you up for."
  )


async def cmd_kyc(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
  """TEMPORARY: enter registration without the deep link."""
  await _enter_kyc(update, context)


async def chat_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
  """Temporary helper: run inside a group to find its chat id."""
  await update.message.reply_text(f"Chat ID: {update.effective_chat.id}")
