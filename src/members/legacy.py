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

from telegram import (InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton,
                      ReplyKeyboardMarkup, ReplyKeyboardRemove, Update)
from telegram.ext import ApplicationHandlerStop, ContextTypes

from src import db
from src.common.telegram_helpers import start_link
from src.config import MAIN_GROUP_ID
from src.members import profile
from src.members.main_group import in_main_group

logger = logging.getLogger(__name__)
# ===========================================================================
# ----- Messages -------------------------------------------------------------
ASK_FOR_PHONE = (
  "If you were registered on the old ATL website, I can bring your details "
  "over so you don't have to type them again.\n\n"
  "Tap below to share your Telegram phone number. I'll use it once to find "
  "your record, and I won't store it.\n\n"
  "Never registered on the old website, or rather not share? Tap Skip."
)
SHARE_BUTTON = "📱 Share my number"
SKIP_BUTTON = "⏭ Skip"
IN_GROUP_NOT_ON_RECORD = ("I can see you're in the main ATL group, but I don't "
                          "have your details on record yet. Let's fix that.")
GROUP_PROMPT = ("Alpha doesn't have your ATL details on record yet. Tap below to "
                "update them. It only takes a couple of minutes.")
PROMPT_DELETE_SECONDS = 15 * 60   # the reply sits in a busy group: keep it brief
PROMPT_EVERY_DAYS = 7             # at most one nudge per person per week
ACTIVATED_NO_RECORD = (
  "✅ You're in the main ATL group, so you're recognised as a member.\n\n"
  "I couldn't find an old record for you, so your profile starts empty. "
  "Tap Fill in missing details below to complete it."
)
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
# ================================================================================================
# ----- Helpers --------------------------------------------------------------
def _link(legacy_id, user, method):
  return db.link_legacy(legacy_id, user.id, user.username,
                        user.first_name, user.last_name, method)


# ----- Route 1: passive username match ---------------------------------------
async def passive_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
  """Group -1. Runs on main-group posts and DMs, before everything else.
  Refreshes a known member's Telegram details (once per restart). Links
  people silently by username where it can. In the main group, anyone
  still not on record gets a brief nudge to update their details. Never
  stops later handlers, so Alpha and KYC carry on as normal."""
  user = update.effective_user
  chat = update.effective_chat
  if user is None or chat is None or user.is_bot:
    return

  checked = context.bot_data.setdefault(_CHECKED, set())
  if user.id in checked:
    return   # one check per person per bot restart, not per message
  checked.add(user.id)

  member = db.get_member(user.id)
  if member is not None:
    # The members table is ATL's source of truth (a vouch is looked up by
    # username there), so keep a known member's Telegram details current.
    db.refresh_telegram_details(user.id, user.username, user.first_name,
                                user.last_name)
  if member is not None and member["status"] == db.STATUS_ACTIVE:
    return

  if user.username and not db.is_legacy_linked(user.id):
    legacy_id = db.find_legacy_match("username_key", db.username_key(user.username))
    if legacy_id is not None and (
        chat.id == MAIN_GROUP_ID or await in_main_group(context.bot, user.id)):
      if _link(legacy_id, user, "username"):
        logger.info("Legacy member linked via username (legacy_id=%s)", legacy_id)
        return

  if chat.id == MAIN_GROUP_ID:
    await _prompt_in_group(update, context)


async def _prompt_in_group(update, context):
  """Reply to a main-group post from someone not on record. Alpha can't DM
  people who haven't started it, so the nudge has to be in the group."""
  user = update.effective_user
  if not db.should_prompt(user.id, PROMPT_EVERY_DAYS):
    return
  sent = await update.effective_message.reply_text(
    GROUP_PROMPT, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(
      "Update my details", url=start_link(context.bot, "link"))]]))
  db.mark_prompted(user.id)
  db.schedule_deletion(sent.chat_id, sent.message_id, PROMPT_DELETE_SECONDS)


# ----- Route 2: one-tap phone match ------------------------------------------
async def handle_link_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
  """Called from /start when the payload is 'link'."""
  user = update.effective_user
  message = update.effective_message

  member = db.get_member(user.id)
  if member is not None and member["status"] == db.STATUS_ACTIVE:
    # Linked already (perhaps by passive_link a moment ago), or a member who
    # registered through the bot. Either way: straight to their profile.
    await profile.show_profile(context.bot, user.id)
    return
  if not await in_main_group(context.bot, user.id):
    await message.reply_text(NOT_IN_MAIN_GROUP)
    return
  await ask_for_phone(update, context)


async def ask_for_phone(update, context, intro=None):
  """Offer Share my number / Skip. Also used by Alpha when a main-group
  member messages it without being on record."""
  context.user_data[_AWAITING] = True
  keyboard = ReplyKeyboardMarkup(
    [[KeyboardButton(SHARE_BUTTON, request_contact=True)],
     [KeyboardButton(SKIP_BUTTON)]],
    resize_keyboard=True, one_time_keyboard=True,
  )
  text = f"{intro}\n\n{ASK_FOR_PHONE}" if intro else ASK_FOR_PHONE
  await update.effective_message.reply_text(text, reply_markup=keyboard)


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
  elif not await in_main_group(context.bot, user.id):
    reply = NOT_IN_MAIN_GROUP
  else:
    legacy_id = db.find_legacy_match("phone_key", db.phone_key(contact.phone_number))
    if legacy_id is not None and _link(legacy_id, user, "phone"):
      logger.info("Legacy member linked via phone (legacy_id=%s)", legacy_id)
      reply = LINKED
    else:
      reply = _activate_without_record(user, "phone, no match")

  await message.reply_text(reply, reply_markup=done)
  if reply in (LINKED, ALREADY_LINKED, ACTIVATED_NO_RECORD):
    await profile.show_profile(context.bot, user.id)
  # Stop here: the phone number must not reach KYC or the LLM.
  raise ApplicationHandlerStop


def _activate_without_record(user, method):
  if db.activate_main_group_member(user.id, user.username, user.first_name,
                                   user.last_name, method):
    logger.info("Main-group member activated without an old record")
    return ACTIVATED_NO_RECORD
  return NO_MATCH   # status needs a human (under review, declined, removed)


async def handle_skip(update: Update, context: ContextTypes.DEFAULT_TYPE):
  """Group -1, private chats: the member tapped Skip instead of sharing
  their number. Only acts if they started the link flow."""
  if not context.user_data.pop(_AWAITING, False):
    return
  user = update.effective_user
  message = update.effective_message
  if not await in_main_group(context.bot, user.id):
    reply = NOT_IN_MAIN_GROUP
  else:
    reply = _activate_without_record(user, "skip")
  await message.reply_text(reply, reply_markup=ReplyKeyboardRemove())
  if reply == ACTIVATED_NO_RECORD:
    await profile.show_profile(context.bot, user.id)
  raise ApplicationHandlerStop
