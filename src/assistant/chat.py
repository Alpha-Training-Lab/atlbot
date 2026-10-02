"""Alpha in DMs: the catch-all for any private text no other handler claimed.

Before asking the LLM, it keeps the members table and the main group in
step (members/main_group.py): removed members are told so, main-group
members with no record are offered the link flow, and active members who
have left get a fresh invite."""
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from src import db
from src.assistant.context import context_for
from src.assistant.llm import ask_alpha
from src.common.telegram_helpers import start_link
from src.members import legacy, main_group
# =============================================
logger = logging.getLogger(__name__)


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

  # --- keep the database and the main group in step ---
  if status == db.STATUS_REMOVED:
    await message.reply_text(main_group.REMOVED_TEXT)
    return

  if status in (None, db.STATUS_PENDING_SUMMARY):
    # In the main group but not on record: get them on record, no LLM needed.
    if await main_group.main_group_state(context.bot, user_id) == main_group.IN:
      await legacy.ask_for_phone(update, context, intro=legacy.IN_GROUP_NOT_ON_RECORD)
      return

  invite = None
  if status == db.STATUS_ACTIVE:
    state = await main_group.main_group_state(context.bot, user_id)
    if state == main_group.BANNED:
      db.set_status(user_id, db.STATUS_REMOVED,
                    note="found removed from the main group")
      await message.reply_text(main_group.REMOVED_TEXT)
      return
    if state == main_group.OUT:
      try:
        invite = await main_group.invite_for(context.bot, user_id)
      except TelegramError:
        logger.exception("Could not create a main-group invite")

  reply = await ask_alpha(message.text, context_note=context_for(member))

  markup = None
  if status == db.STATUS_AWAITING_DM:
    markup = InlineKeyboardMarkup([[
      InlineKeyboardButton("Start registration", callback_data="rst:begin")
    ]])
  elif status == db.STATUS_ACTIVE:
    markup = InlineKeyboardMarkup([[
      InlineKeyboardButton("👤 My profile", callback_data="pf:menu")
    ]])
  elif status in (None, db.STATUS_PENDING_SUMMARY):
    markup = InlineKeyboardMarkup([[InlineKeyboardButton(
      "I'm already an ATL member", url=start_link(context.bot, "link"))]])
  await message.reply_text(reply, reply_markup=markup)
  if invite:
    await message.reply_text(main_group.REJOIN_TEXT, reply_markup=InlineKeyboardMarkup([[
      InlineKeyboardButton("Join the main group", url=invite)]]))
