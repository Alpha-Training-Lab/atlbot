"""Induction group listener: welcome, catch the tag, first admin gate."""
import logging
import re

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from src import db
from src.messages import WELCOME_INDUCTION, INDUCTION_POST_INCOMPLETE
from config import (
  INDUCTION_GROUP_ID, ONBOARDING_GROUP_ID,
  INDUCTION_PINNED_URL, MIN_INDUCTION_SECONDS,
  WELCOME_DELETE_SECONDS, REMINDER_DELETE_SECONDS,
  REQUIRED_TAGS,
)
# ===============================================================================
logger = logging.getLogger(__name__)

# ===============================================================================
def _message_link(chat_id, message_id):
  """Supergroup message link: strip the -100 prefix."""
  return f"https://t.me/c/{str(chat_id).replace('-100', '', 1)}/{message_id}"


def _mention(user_id, name):
  return f'<a href="tg://user?id={user_id}">{name}</a>'

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

    reminder = await message.reply_text(
      "Quick reminder: take your time with the material — most people "
      "spend one to two weeks on it. When you're ready, come back here "
      "and tag me; the pinned instructions explain what your post needs "
      "to include."
    )
    db.schedule_deletion(reminder.chat_id, reminder.message_id,
                         REMINDER_DELETE_SECONDS)


async def handle_join_request(update, context):
  req = update.chat_join_request
  user_id = req.from_user.id
  member = db.get_member(user_id)

  if member and member["status"] == db.STATUS_ACTIVE:
    await context.bot.approve_chat_join_request(req.chat.id, user_id)
    db.log_event(user_id, "joined_main_group")
  else:
    await context.bot.decline_chat_join_request(req.chat.id, user_id)
    db.log_event(user_id, "join_request_declined")


# --- 2. catch the tag --------------------------------------------------
def _bot_was_tagged(message, bot_username):
  if message.reply_to_message and message.reply_to_message.from_user:
    if message.reply_to_message.from_user.is_bot:
      return True
  text = message.text or ""
  return f"@{bot_username}".lower() in text.lower()


async def handle_induction_post(update, context: ContextTypes.DEFAULT_TYPE):
  message = update.message
  if message is None or message.chat.id != INDUCTION_GROUP_ID:
    return
  if not message.text:
    return
  if not _bot_was_tagged(message, context.bot.username):
    return

  user = update.effective_user
  db.upsert_member(user.id, username=user.username,
                   first_name=user.first_name, last_name=user.last_name)
  member = db.get_member(user.id)
  status = member["status"]

  if status == db.STATUS_PENDING_REVIEW:
    await message.reply_text(
      "You're already in the queue. An admin will get to you — "
      "tagging again won't speed it up.")
    return

  if status == db.STATUS_AWAITING_DM:
    await message.reply_text(
      "You've already been approved. Tap below to register with me.",
      reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(
        "Start registration",
        url=f"https://t.me/{context.bot.username}?start=kyc")]]))
    return

  if status in (db.STATUS_KYC_IN_PROGRESS, db.STATUS_PENDING_ACCESS,
                db.STATUS_ACTIVE, db.STATUS_DECLINED):
    await message.reply_text(
      "You're already past this stage — message me directly and I'll "
      "tell you where you stand.")
    return

  # --- the main path: pending_summary ---
  observed = db.seconds_since_last_event(user.id, "joined_induction")

  if observed is not None and observed < MIN_INDUCTION_SECONDS:
    remaining = MIN_INDUCTION_SECONDS - observed
    await message.reply_text(
      f"Thanks {user.first_name or ''} — but you joined recently. "
      f"Members spend at least {_humanise(MIN_INDUCTION_SECONDS)} on the "
      f"induction material. Come back in about {_humanise(remaining)} "
      "and tag me again.")
    return

  missing = _missing_tags(message.text)
  if missing:
    db.log_event(user.id, "induction_post_incomplete")
    sent = await message.reply_text(
      INDUCTION_POST_INCOMPLETE.format(
        name=user.first_name or "",
        url=INDUCTION_PINNED_URL,
      ),
      disable_web_page_preview=True,
    )
    db.schedule_deletion(sent.chat_id, sent.message_id,
                         REMINDER_DELETE_SECONDS)
    return

  app_id = db.create_application(
    user.id, message.text,
    source_chat_id=message.chat.id,
    source_message_id=message.message_id,
  )
  db.set_status(user.id, db.STATUS_PENDING_REVIEW)

  await message.reply_text(
    "Got it — thank you. Your request has gone to the onboarding team. "
    "They'll review it when they next check, and I'll post here once "
    "there's a decision. No need to message anyone in the meantime.")

  await send_summary_card(context.bot, app_id, observed)


# --- 3. admin review card ----------------------------------------------
async def send_summary_card(bot, app_id, observed_seconds):
  app = db.get_application(app_id)
  member = db.get_member(app["user_id"])

  handle = f"@{member['username']}" if member["username"] else "(no username)"
  name = f"{member['first_name'] or ''} {member['last_name'] or ''}".strip()

  if observed_seconds is None:
    timing = "⏱ Time in group UNKNOWN — joined before the bot. Use your judgement."
  else:
    timing = f"⏱ In group {int(observed_seconds // 86400)} days (bot-observed)"

  body = (app["summary_text"] or "")[:1500]

  text = (
    "INDUCTION REQUEST — awaiting review\n\n"
    f"Telegram: {name} {handle}\n"
    f"User ID: {app['user_id']}\n"
    f"{timing}\n\n"
    f"What they posted:\n{body}\n\n"
    f"Original: {_message_link(app['source_chat_id'], app['source_message_id'])}"
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
  await query.answer()

  if query.message is None or query.message.chat.id != ONBOARDING_GROUP_ID:
    return

  parts = (query.data or "").split(":")
  if len(parts) < 3:
    logger.warning("Unparseable induction callback: %r", query.data)
    return

  action = parts[1]
  try:
    app_id = int(parts[2])
  except ValueError:
    logger.warning("Bad application id: %r", query.data)
    return

  admin = query.from_user
  admin_name = admin.first_name or str(admin.id)

  decision = "approved" if action == "approve" else "declined"
  if not db.decide_application(app_id, decision, admin.id):
    await query.answer("Already handled.", show_alert=True)
    await query.edit_message_reply_markup(reply_markup=None)
    return

  app = db.get_application(app_id)
  user_id = app["user_id"]
  member = db.get_member(user_id)
  name = member["first_name"] or "there"

  if action == "approve":
    db.set_status(user_id, db.STATUS_AWAITING_DM, actor_user_id=admin.id)
    await query.edit_message_text(
      f"{query.message.text}\n\n✅ APPROVED by {admin_name}",
      disable_web_page_preview=True)
    await context.bot.send_message(
      chat_id=INDUCTION_GROUP_ID,
      text=(f"{_mention(user_id, name)}, you've been approved. "
            "Tap below to register with me privately."),
      parse_mode=ParseMode.HTML,
      reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(
        "Start registration",
        url=f"https://t.me/{context.bot.username}?start=kyc")]]),
    )
  else:
    db.set_status(user_id, db.STATUS_PENDING_SUMMARY, actor_user_id=admin.id)
    await query.edit_message_text(
      f"{query.message.text}\n\n❌ NOT YET — by {admin_name}",
      disable_web_page_preview=True)
    await context.bot.send_message(
      chat_id=INDUCTION_GROUP_ID,
      text=(f"{_mention(user_id, name)}, please spend more time with the "
            f"induction material: {INDUCTION_PINNED_URL}\n\n"
            "Tag me again when you're ready."),
      parse_mode=ParseMode.HTML,
      disable_web_page_preview=True,
    )


# ----- 5. sweep deletions ------------------------------------------------
async def sweep_deletions(context: ContextTypes.DEFAULT_TYPE):
  for row in db.due_deletions():
    try:
      await context.bot.delete_message(row["chat_id"], row["message_id"])
    except Exception as e:
      logger.warning("Could not delete %s in %s: %s",
                     row["message_id"], row["chat_id"], e)
    db.clear_deletion(row["id"])   # clear either way — don't retry forever


