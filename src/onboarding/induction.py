"""The induction group: welcome new arrivals, catch the "I'm ready" post,
and let admins approve it before registration (kyc.py) can begin."""
import logging
import re

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from src import db
from src.assistant.context import context_for
from src.assistant.llm import ask_alpha, classify_induction_intent
from src.common.telegram_helpers import mention, message_link, start_link, who
from src.config import (
  INDUCTION_GROUP_ID, ONBOARDING_GROUP_ID,
  INDUCTION_PINNED_URL, MIN_INDUCTION_SECONDS,
  WELCOME_DELETE_SECONDS, REMINDER_DELETE_SECONDS,
  REGISTRATION_PROMPT_DELETE_SECONDS,
  REQUIRED_TAGS,
)
from src.members.main_group import REMOVED_TEXT
from src.onboarding.messages import INDUCTION_POST_INCOMPLETE, WELCOME_INDUCTION
# ===============================================================================
logger = logging.getLogger(__name__)

GROUP_REPLY_CONTEXT = (
  "\n\nYou are replying publicly in the ATL induction group, not in a "
  "private chat. Keep the reply short — three sentences at most. Never "
  "post a group invite link here. If they need the induction material, "
  f"point them to {INDUCTION_PINNED_URL}."
)
# ===============================================================================


def _humanise(seconds):
  seconds = int(seconds)
  if seconds >= 86400:
    n = -(-seconds // 86400)          # ceiling division
    return f"{n} day{'s' if n != 1 else ''}"
  if seconds >= 3600:
    n = -(-seconds // 3600)
    return f"{n} hour{'s' if n != 1 else ''}"
  n = max(1, -(-seconds // 60))
  return f"{n} minute{'s' if n != 1 else ''}"


def _missing_tags(text):
  """Required admin handles absent from the post."""
  missing = []
  for tag in REQUIRED_TAGS:
    if not re.search(re.escape(tag) + r"\b", text or "", re.IGNORECASE):
      missing.append(tag)
  return missing


def _schedule_pair(user_message, bot_message, seconds):
  """Delete both the member's post and Alpha's reply together."""
  db.schedule_deletion(bot_message.chat_id, bot_message.message_id, seconds)
  db.schedule_deletion(user_message.chat_id, user_message.message_id, seconds)


# --- 1. welcome on join ------------------------------------------------
async def handle_new_member(update, context: ContextTypes.DEFAULT_TYPE):
  message = update.message
  if message is None or message.chat.id != INDUCTION_GROUP_ID:
    return

  for user in message.new_chat_members or []:
    if user.is_bot:
      continue
    db.upsert_member(user.id, username=user.username,
                     first_name=user.first_name, last_name=user.last_name)
    db.log_event(user.id, "joined_induction")   # bot WATCHED them arrive

    welcome = await message.reply_text(
      WELCOME_INDUCTION.format(name=user.first_name or "friend",
                               url=INDUCTION_PINNED_URL),
      disable_web_page_preview=True,
    )
    db.schedule_deletion(welcome.chat_id, welcome.message_id,
                         WELCOME_DELETE_SECONDS)


# --- 2. catch the tag --------------------------------------------------
def _bot_was_tagged(message, bot):
  """True if THIS bot was mentioned or replied to."""
  replied = message.reply_to_message
  if replied and replied.from_user and replied.from_user.id == bot.id:
    return True
  return f"@{bot.username}".lower() in (message.text or "").lower()


async def handle_induction_post(update, context: ContextTypes.DEFAULT_TYPE):
  message = update.message
  if message is None or message.chat.id != INDUCTION_GROUP_ID:
    return
  if not message.text:
    return
  if not _bot_was_tagged(message, context.bot):
    return

  user = update.effective_user
  db.upsert_member(user.id, username=user.username,
                   first_name=user.first_name, last_name=user.last_name)
  member = db.get_member(user.id)
  status = member["status"]

  if status == db.STATUS_PENDING_REVIEW:
    sent = await message.reply_text(
      "You're already in the queue. An admin will get to you — "
      "tagging again won't speed it up.")
    _schedule_pair(message, sent, REMINDER_DELETE_SECONDS)
    return

  if status == db.STATUS_AWAITING_DM:
    sent = await message.reply_text(
      "You've already been approved. Tap below to register with me.",
      reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(
        "Start registration", url=start_link(context.bot, "kyc"))]]))
    _schedule_pair(message, sent, REMINDER_DELETE_SECONDS)
    return

  if status == db.STATUS_REMOVED:
    sent = await message.reply_text(REMOVED_TEXT)
    _schedule_pair(message, sent, REMINDER_DELETE_SECONDS)
    return

  if status in (db.STATUS_KYC_IN_PROGRESS, db.STATUS_PENDING_ACCESS,
                db.STATUS_ACTIVE, db.STATUS_DECLINED):
    sent = await message.reply_text(
      "You're already past this stage — message me directly and I'll "
      "tell you where you stand.")
    _schedule_pair(message, sent, REMINDER_DELETE_SECONDS)
    return

  # --- the main path: pending_summary ---
  missing = _missing_tags(message.text)
  intent = "onboarding"
  if missing:
    intent = await classify_induction_intent(message.text)

  if intent == "question":
    reply = await ask_alpha(
      message.text,
      context_note=context_for(member) + GROUP_REPLY_CONTEXT,
    )
    sent = await message.reply_text(reply, disable_web_page_preview=True)
    _schedule_pair(message, sent, REMINDER_DELETE_SECONDS)
    return

  # --- onboarding intent from here ---
  observed = db.seconds_since_last_event(user.id, "joined_induction")

  if observed is not None and observed < MIN_INDUCTION_SECONDS:
    remaining = MIN_INDUCTION_SECONDS - observed
    sent = await message.reply_text(
      f"Thanks {user.first_name or ''} — but you joined recently. "
      f"Members spend at least {_humanise(MIN_INDUCTION_SECONDS)} on the "
      f"induction material. Come back in about {_humanise(remaining)} "
      "and tag me again.")
    _schedule_pair(message, sent, REMINDER_DELETE_SECONDS)
    return

  if missing:
    db.log_event(user.id, "induction_post_incomplete")
    sent = await message.reply_text(
      INDUCTION_POST_INCOMPLETE.format(name=user.first_name or "",
                                       url=INDUCTION_PINNED_URL),
      disable_web_page_preview=True)
    _schedule_pair(message, sent, REMINDER_DELETE_SECONDS)
    return

  app_id = db.create_application(
    user.id, message.text,
    source_chat_id=message.chat.id,
    source_message_id=message.message_id,
  )
  db.set_status(user.id, db.STATUS_PENDING_REVIEW)

  sent = await message.reply_text(
    "Got it — thank you. Your request has gone to the onboarding team. "
    "They'll review it when they next check, and I'll post here once "
    "there's a decision. No need to message anyone in the meantime.")
  # Alpha's ack goes on a timer; the member's post waits for the decision.
  db.schedule_deletion(sent.chat_id, sent.message_id, WELCOME_DELETE_SECONDS)

  await send_summary_card(context.bot, app_id, observed)


# --- 3. admin review card ----------------------------------------------
async def send_summary_card(bot, app_id, observed_seconds):
  app = db.get_application(app_id)
  member = db.get_member(app["user_id"])

  if observed_seconds is None:
    timing = ("⏱ Time in group UNKNOWN — joined before the bot. "
              "Use your judgement.")
  else:
    timing = f"⏱ In group {_humanise(observed_seconds)} (bot-observed)"

  text = (
    "INDUCTION REQUEST — awaiting review\n\n"
    f"Telegram: {who(member)}\n"
    f"{timing}\n\n"
    f"Read their post: "
    f"{message_link(app['source_chat_id'], app['source_message_id'])}"
  )

  await bot.send_message(
    chat_id=ONBOARDING_GROUP_ID,
    text=text,
    disable_web_page_preview=True,
    reply_markup=InlineKeyboardMarkup([[
      InlineKeyboardButton("✅ Approve", callback_data=f"ind:approve:{app_id}"),
      InlineKeyboardButton("❌ Not yet", callback_data=f"ind:reject:{app_id}"),
    ]]),
  )


# --- 4. the decision ---------------------------------------------------
async def handle_induction_decision(update, context: ContextTypes.DEFAULT_TYPE):
  query = update.callback_query
  if query.message is None or query.message.chat.id != ONBOARDING_GROUP_ID:
    await query.answer()
    return

  parts = (query.data or "").split(":")
  try:
    action, app_id = parts[1], int(parts[2])
  except (IndexError, ValueError):
    logger.warning("Unparseable induction callback: %r", query.data)
    await query.answer()
    return

  admin = query.from_user
  admin_name = admin.first_name or str(admin.id)

  # Expelling closes their open application; this is the backstop, since
  # either decision below would move them out of removed.
  pending = db.get_application(app_id)
  owner = db.get_member(pending["user_id"]) if pending else None
  if owner is not None and owner["status"] == db.STATUS_REMOVED:
    await query.answer("They've been expelled.", show_alert=True)
    await query.edit_message_reply_markup(reply_markup=None)
    return

  decision = "approved" if action == "approve" else "declined"
  if not db.decide_application(app_id, decision, admin.id):
    await query.answer("Already handled.", show_alert=True)
    await query.edit_message_reply_markup(reply_markup=None)
    return
  await query.answer()

  app = db.get_application(app_id)
  user_id = app["user_id"]
  member = db.get_member(user_id)
  name = member["first_name"] or "there"

  # Remove the member's post so the correct tag list isn't left on display.
  try:
    await context.bot.delete_message(app["source_chat_id"],
                                     app["source_message_id"])
  except Exception as e:
    logger.warning("Could not delete induction post %s: %s",
                   app["source_message_id"], e)

  if action == "approve":
    db.set_status(user_id, db.STATUS_AWAITING_DM, actor_user_id=admin.id)
    await query.edit_message_text(
      f"{query.message.text}\n\n✅ APPROVED by {admin_name}",
      disable_web_page_preview=True)
    sent = await context.bot.send_message(
      chat_id=INDUCTION_GROUP_ID,
      text=(f"{mention(user_id, name)}, you've been approved. "
            "Tap below to register with me privately."),
      parse_mode=ParseMode.HTML,
      reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(
        "Start registration", url=start_link(context.bot, "kyc"))]]),
    )
    # Deleted as soon as they start registering (kyc.py), or after
    # the fallback period if they never tap it.
    db.save_registration_prompt(user_id, sent.chat_id, sent.message_id)
    db.schedule_deletion(sent.chat_id, sent.message_id,
                         REGISTRATION_PROMPT_DELETE_SECONDS)
  else:
    db.set_status(user_id, db.STATUS_PENDING_SUMMARY, actor_user_id=admin.id)
    await query.edit_message_text(
      f"{query.message.text}\n\n❌ NOT YET — by {admin_name}",
      disable_web_page_preview=True)
    sent = await context.bot.send_message(
      chat_id=INDUCTION_GROUP_ID,
      text=(f"{mention(user_id, name)}, please spend more time with the "
            f"induction material: {INDUCTION_PINNED_URL}\n\n"
            "Tag me again when you're ready."),
      parse_mode=ParseMode.HTML,
      disable_web_page_preview=True,
    )
    db.schedule_deletion(sent.chat_id, sent.message_id, REMINDER_DELETE_SECONDS)

