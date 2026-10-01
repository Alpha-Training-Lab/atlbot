"""Alpha conversational module — routes member DMs to the LLM."""
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from src import db
from src.modules import legacy, membership
from src.llm import ask_alpha
# =============================================
logger = logging.getLogger(__name__)

MODULE = "alpha"
STATUS_CONTEXT = {
  db.STATUS_PENDING_SUMMARY:
    "This member has NOT yet been approved out of the induction group. "
    "Their next step is to finish reading the induction material, then "
    "post in the induction group tagging the onboarding admins. "
    "Registration cannot begin until an admin approves them. "
    "BUT if they say they are already in the main ATL group, believe that "
    "they may be right: tell them to tap the 'I'm already an ATL member' "
    "button below, which checks with Telegram and recognises them.",
  db.STATUS_PENDING_REVIEW:
    "This member has posted in the induction group and an admin is "
    "reviewing them. They only need to wait.",
  db.STATUS_AWAITING_DM:
    "This member has been approved out of induction. They should tap the "
    "button in the induction group to start registration with you.",
  db.STATUS_KYC_IN_PROGRESS:
    "This member is part-way through registration. Tell them to answer "
    "the question you last asked.",
  db.STATUS_PENDING_ACCESS:
    "This member has finished registration. The admin team is doing the "
    "final review. They only need to wait.",
  db.STATUS_ACTIVE:
    "This member is fully approved and has group access. If they want to "
    "see, complete or update their personal details, tell them to tap the "
    "My profile button below or send /profile. Never ask them to type "
    "personal details to you in this chat.",
  db.STATUS_DECLINED:
    "This member's registration was declined. They can fix the issue and "
    "submit again using the button already sent to them.",
}
REMOVED_TEXT = ("Your ATL membership has been removed. If you think this is a "
                "mistake, please contact an ATL admin.")
REJOIN_TEXT = ("You're not in the main ATL group at the moment. Here's your "
               "personal link to rejoin. It works once and expires in 48 hours.")

NO_RECORD = (
  "This person has no record in the ATL database. If they say they are "
  "already an ATL member, tell them to tap the 'I'm already an ATL member' "
  "button below so you can find their record. Otherwise they have not "
  "started onboarding, and their first step is the induction group."
)
# =============================================================================


def _context_for(member):
  if member is None:
    return NO_RECORD
  return STATUS_CONTEXT.get(member["status"], NO_RECORD)


async def handle_alpha_message(
  update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
  message = update.message
  if not message or not message.text:
    return

  user_id = update.effective_user.id
  logger.info("Alpha query from user %s", user_id)

  await context.bot.send_chat_action(
    chat_id=message.chat_id, action=ChatAction.TYPING
  )

  member = db.get_member(user_id)
  status = member["status"] if member else None

  # --- keep the database and the main group in step (membership.py) ---
  if status == db.STATUS_REMOVED:
    await message.reply_text(REMOVED_TEXT)
    return

  if status in (None, db.STATUS_PENDING_SUMMARY):
    # In the main group but not on record: get them on record, no LLM needed.
    if await membership.main_group_state(context.bot, user_id) == membership.IN:
      await legacy.ask_for_phone(update, context, intro=legacy.IN_GROUP_NOT_ON_RECORD)
      return

  invite = None
  if status == db.STATUS_ACTIVE:
    state = await membership.main_group_state(context.bot, user_id)
    if state == membership.BANNED:
      db.set_status(user_id, db.STATUS_REMOVED,
                    note="found removed from the main group")
      await message.reply_text(REMOVED_TEXT)
      return
    if state == membership.OUT:
      try:
        invite = await membership.invite_for(context.bot, user_id)
      except TelegramError:
        logger.exception("Could not create a main-group invite")

  reply = await ask_alpha(message.text, context_note=_context_for(member))

  markup = None
  if member and member["status"] == db.STATUS_AWAITING_DM:
    markup = InlineKeyboardMarkup([[
      InlineKeyboardButton("Start registration", callback_data="rst:begin")
    ]])
  elif member and member["status"] == db.STATUS_ACTIVE:
    markup = InlineKeyboardMarkup([[
      InlineKeyboardButton("👤 My profile", callback_data="pf:menu")
    ]])
  elif member is None or member["status"] == db.STATUS_PENDING_SUMMARY:
    markup = InlineKeyboardMarkup([[InlineKeyboardButton(
      "I'm already an ATL member",
      url=f"https://t.me/{context.bot.username}?start=link")]])
  await message.reply_text(reply, reply_markup=markup)
  if invite:
    await message.reply_text(REJOIN_TEXT, reply_markup=InlineKeyboardMarkup([[
      InlineKeyboardButton("Join the main group", url=invite)]]))