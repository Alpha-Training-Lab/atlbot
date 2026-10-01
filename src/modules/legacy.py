"""Link legacy members (imported from the old website's spreadsheet) to their
Telegram accounts, so Alpha recognises them instead of sending them through
induction.

Two routes:
  1. Passive, no effort from the member: the first message we see from
     someone in the main group, or in a DM, is checked against the
     spreadsheet's Telegram usernames.
  2. One tap: t.me/AlphaTrainingLab_bot?start=link asks the member to share
     their Telegram phone number, matched against the spreadsheet's phones.
     The number is used for matching only and is never stored or logged.
"""
import logging
import re

from telegram import KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove, Update
from telegram.constants import ChatMemberStatus
from telegram.error import TelegramError
from telegram.ext import ApplicationHandlerStop, ContextTypes

from config import MAIN_GROUP_ID
from src import db

logger = logging.getLogger(__name__)
# ===========================================================================
# ----- Messages -------------------------------------------------------------
ASK_FOR_PHONE = (
  "If you've been an ATL member for a while, I can find your existing record "
  "so you don't have to register again.\n\n"
  "Tap the button below to share your Telegram phone number. I'll use it "
  "once to find your record, and I won't store it."
)
SHARE_BUTTON = "📱 Share my number"
LINKED = ("✅ Found you. You're recognised as an existing ATL member, "
          "so there's no need to go through registration.")
ALREADY_LINKED = "✅ You're already recognised as an existing ATL member. Nothing more to do."
NO_MATCH = ("I couldn't match that number to an existing ATL record. If you've "
            "been a member for a while, please contact an ATL admin and they'll "
            "sort it out.")
NOT_IN_MAIN_GROUP = ("This link is for existing members of the main ATL group. "
                     "If you're new, please use the link shared in the induction group.")
OWN_NUMBER_ONLY = "Please use the button to share your own number."

_AWAITING = "awaiting_link_contact"   # user_data flag: they tapped ?start=link
_CHECKED = "legacy_checked"           # bot_data: user ids already checked this run
_IN_GROUP = {ChatMemberStatus.OWNER, ChatMemberStatus.ADMINISTRATOR,
             ChatMemberStatus.MEMBER}
# ================================================================================================
# ----- Normalisers ----------------------------------------------------------
# These MUST match the ones in scripts/import_legacy.py, or nothing matches.
def username_key(raw):
  v = (raw or "").strip().lower().lstrip("@")
  return v if re.fullmatch(r"[a-z0-9_]{5,32}", v) else None


def phone_key(raw):
  digits = re.sub(r"\D", "", raw or "")
  return digits[-10:] if len(digits) >= 10 else None


# ----- Helpers --------------------------------------------------------------

async def _in_main_group(bot, user_id):
  """A DM proves nothing about membership, so ask Telegram."""
  if not MAIN_GROUP_ID:
    logger.warning("MAIN_GROUP_ID is not set; legacy linking is disabled")
    return False
  try:
    member = await bot.get_chat_member(MAIN_GROUP_ID, user_id)
  except TelegramError:
    logger.warning("Could not check main-group membership", exc_info=True)
    return False
  if member.status == ChatMemberStatus.RESTRICTED:
    return bool(getattr(member, "is_member", False))
  return member.status in _IN_GROUP


def _link(legacy_id, user, method):
  return db.link_legacy(legacy_id, user.id, user.username,
                        user.first_name, user.last_name, method)


# ----- Route 1: passive username match ---------------------------------------

async def passive_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
  """Group -1. Never replies and never stops later handlers, so Alpha and
  KYC carry on as normal, just with the member already linked."""
  user = update.effective_user
  chat = update.effective_chat
  if user is None or chat is None or user.is_bot or not user.username:
    return

  checked = context.bot_data.setdefault(_CHECKED, set())
  if user.id in checked:
    return   # one database check per person per bot restart, not per message
  checked.add(user.id)

  if db.is_legacy_linked(user.id):
    return
  legacy_id = db.find_legacy_match("username_key", username_key(user.username))
  if legacy_id is None:
    return
  if chat.id != MAIN_GROUP_ID and not await _in_main_group(context.bot, user.id):
    return
  if _link(legacy_id, user, "username"):
    logger.info("Legacy member linked via username (legacy_id=%s)", legacy_id)


# ----- Route 2: one-tap phone match ------------------------------------------

async def handle_link_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
  """Called from /start when the payload is 'link'."""
  user = update.effective_user
  message = update.effective_message

  if db.is_legacy_linked(user.id):   # passive_link may have just done it
    await message.reply_text(ALREADY_LINKED)
    return
  if not await _in_main_group(context.bot, user.id):
    await message.reply_text(NOT_IN_MAIN_GROUP)
    return

  context.user_data[_AWAITING] = True
  keyboard = ReplyKeyboardMarkup(
    [[KeyboardButton(SHARE_BUTTON, request_contact=True)]],
    resize_keyboard=True, one_time_keyboard=True,
  )
  await message.reply_text(ASK_FOR_PHONE, reply_markup=keyboard)


async def handle_contact(update: Update, context: ContextTypes.DEFAULT_TYPE):
  """Group -1, private chats. Only acts if the member started the link flow;
  otherwise the contact passes through to the other handlers untouched."""
  if not context.user_data.pop(_AWAITING, False):
    return

  user = update.effective_user
  message = update.effective_message
  contact = message.contact
  done = ReplyKeyboardRemove()

  if contact.user_id != user.id:   # a forwarded contact card, not their own
    context.user_data[_AWAITING] = True   # let them try again
    await message.reply_text(OWN_NUMBER_ONLY)
    raise ApplicationHandlerStop

  if db.is_legacy_linked(user.id):
    reply = ALREADY_LINKED
  elif not await _in_main_group(context.bot, user.id):
    reply = NOT_IN_MAIN_GROUP
  else:
    legacy_id = db.find_legacy_match("phone_key", phone_key(contact.phone_number))
    if legacy_id is not None and _link(legacy_id, user, "phone"):
      logger.info("Legacy member linked via phone (legacy_id=%s)", legacy_id)
      reply = LINKED
    else:
      reply = NO_MATCH

  await message.reply_text(reply, reply_markup=done)
  # Stop here: the phone number must not reach KYC or the LLM.
  raise ApplicationHandlerStop