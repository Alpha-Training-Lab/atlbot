"""Step 1 of the weekly brief: store each text message posted in a group
the brief reads, and keep track of which groups those are.

main.py registers these in handler group -3, before every other handler.
Nothing here raises ApplicationHandlerStop, so every other handler still
sees every message.

Which groups are read:
  - the main group and config.BRIEF_READ_GROUPS (seed_groups, at startup);
  - any group an admin or the owner adds Alpha to, or promotes it to admin
    in;
  - never the groups in config.BRIEF_NEVER_READ.

/briefgroups (owner, in a DM) shows which groups are read and whether
Alpha can actually see their messages.

The felicitation group is read for birthdays only (capture_birthday): no
text is stored, just who was wished a happy birthday.
"""
import logging
import re
from datetime import date, timedelta, timezone

from telegram import MessageEntity, Update
from telegram.constants import ChatMemberStatus, ChatType
from telegram.ext import ContextTypes

from src import db
from src.config import (BRIEF_NEVER_READ, BRIEF_READ_GROUPS, FELICITATION_GROUP_ID,
                        INDUCTION_GROUP_ID, LEADERSHIP_GROUP_ID, MAIN_GROUP_ID,
                        ONBOARDING_GROUP_ID)
from src.members import roles
# ===========================================================================
logger = logging.getLogger(__name__)

_IN_GROUP = {ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR}


def seed_groups():
  """Groups Alpha was in before this feature existed: it never saw itself
  being added, so they're recorded here, at every startup."""
  if MAIN_GROUP_ID and MAIN_GROUP_ID not in BRIEF_NEVER_READ:
    db.ensure_brief_group(MAIN_GROUP_ID, enabled=True)
  for chat_id in BRIEF_READ_GROUPS:
    if chat_id in BRIEF_NEVER_READ:
      logger.warning("Brief: group %s is set to be read but is never read", chat_id)
    else:
      db.enable_brief_group(chat_id)


def _reads(chat_id):
  return chat_id not in BRIEF_NEVER_READ and db.brief_group_enabled(chat_id)


def _replied_to(message):
  """Who this message answers, if it should count toward "most helpful".
  Not a bot (members reply to Alpha all the time), not themselves."""
  target = message.reply_to_message
  if target is None or target.from_user is None:
    return None
  # In a group with Topics, every message "replies" to its topic's first post.
  if target.forum_topic_created is not None:
    return None
  if target.from_user.is_bot or target.from_user.id == message.from_user.id:
    return None
  return target.from_user.id


async def capture_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
  message = update.message
  if message is None or not message.text:
    return
  sender = message.from_user
  if sender is None or sender.is_bot:   # also skips anonymous admins
    return
  try:
    if not _reads(message.chat_id):
      return
    db.save_brief_message(
      message.chat_id, message.message_id, sender.id, _replied_to(message),
      message.text,
      message.date.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
    )
  except Exception:
    # Never let the brief break anything else. No message text in the log.
    logger.exception("Brief: could not store a message from chat %s",
                     message.chat_id)


# A birthday wish: "happy birthday", "happy b'day", "HBD", "many happy returns".
_BIRTHDAY_WISH = re.compile(
  r"happy\s*(birth\s*day|b'?day|bday)|\bhbd\b|many happy returns", re.IGNORECASE)
_MENTIONS = [MessageEntity.MENTION, MessageEntity.TEXT_MENTION]


def _celebrants(message, bot_username):
  """Who a birthday post is for: everyone it @mentions, except Alpha."""
  if message.text:
    found = message.parse_entities(_MENTIONS)
  else:
    found = message.parse_caption_entities(_MENTIONS)
  people = set()
  for entity, text in found.items():
    if entity.type == MessageEntity.TEXT_MENTION:      # a name linked to a user
      if entity.user and not entity.user.is_bot:
        people.add(f"id:{entity.user.id}")
    else:                                              # an @username
      username = text.lstrip("@").lower()
      if username and username != (bot_username or "").lower():
        people.add(f"u:{username}")
  return people


async def capture_birthday(update: Update, context: ContextTypes.DEFAULT_TYPE):
  """A post in the felicitation group (text, or a picture's caption)."""
  message = update.message
  if message is None:
    return
  words = message.text or message.caption or ""
  if not _BIRTHDAY_WISH.search(words):
    return
  try:
    people = _celebrants(message, context.bot.username)
    if people:
      day = message.date.astimezone(timezone.utc).strftime("%Y-%m-%d")
      db.save_birthdays(day, people)
  except Exception:
    logger.exception("Brief: could not record a birthday")


async def track_alpha_groups(update: Update, context: ContextTypes.DEFAULT_TYPE):
  """Alpha was added to, or left, a group."""
  change = update.my_chat_member
  if change is None or change.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
    return
  chat_id = change.chat.id
  old, new = change.old_chat_member.status, change.new_chat_member.status
  was_in, now_in = old in _IN_GROUP, new in _IN_GROUP
  # Promoted to admin in a group it was already in: treated like being added.
  promoted = (was_in and old != ChatMemberStatus.ADMINISTRATOR
              and new == ChatMemberStatus.ADMINISTRATOR)

  if (now_in and not was_in) or promoted:
    adder = change.from_user.id if change.from_user else None
    if chat_id == FELICITATION_GROUP_ID:
      enabled, why = False, "birthdays only"
    elif chat_id in BRIEF_NEVER_READ:
      enabled, why = False, "OFF (never read)"
    elif adder is not None and roles.is_admin(adder):
      enabled, why = True, "ON"
    else:
      enabled, why = False, "OFF (not added by an admin)"
    if promoted and why.startswith("OFF (not added") and db.brief_group_enabled(chat_id):
      return   # already read; a non-admin promoting Alpha doesn't switch it off
    db.set_brief_group(chat_id, enabled, adder)
    logger.info("Brief: Alpha %s in group %s, reading %s",
                "promoted" if promoted else "added", chat_id, why)
  elif was_in and not now_in:
    db.remove_brief_group(chat_id)
    logger.info("Brief: Alpha left group %s", chat_id)


# ----- /briefgroups ----------------------------------------------------------
async def _describe(bot, chat_id, reads_all):
  """'Title: Alpha is admin' (or why it can't see messages)."""
  try:
    title = (await bot.get_chat(chat_id)).title or str(chat_id)
  except Exception:
    return f"{chat_id}: Alpha isn't in this group (or it doesn't exist)", False
  try:
    status = (await bot.get_chat_member(chat_id, bot.id)).status
  except Exception:
    status = None
  if status == ChatMemberStatus.ADMINISTRATOR:
    return f"{title}: Alpha is admin", True
  if status in _IN_GROUP and reads_all:
    return f"{title}: Alpha is a member (privacy mode off)", True
  if status in _IN_GROUP:
    return f"{title}: Alpha isn't admin, so it only sees messages that tag it", False
  return f"{title}: Alpha isn't in this group", False


async def cmd_brief_groups(update: Update, context: ContextTypes.DEFAULT_TYPE):
  """Owner only: which groups the weekly brief reads, and is it working?"""
  bot = context.bot
  reads_all = bool((await bot.get_me()).can_read_all_group_messages)
  today = db.brief_messages_today()
  read, off = [], []
  rows = sorted(db.brief_groups(), key=lambda r: r["chat_id"] != MAIN_GROUP_ID)
  for row in rows:
    chat_id = row["chat_id"]
    if chat_id in BRIEF_NEVER_READ:
      continue
    line, sees = await _describe(bot, chat_id, reads_all)
    if row["enabled"]:
      icon = "✅" if sees else "⚠️"
      read.append(f"{icon} {line} · {today.get(chat_id, 0):,} messages stored today")
    else:
      off.append(f"• {line} (not added by an admin)")

  parts = ["GROUPS THE WEEKLY BRIEF READS\n" + ("\n".join(read) or "None yet.")]
  if FELICITATION_GROUP_ID:
    line, sees = await _describe(bot, FELICITATION_GROUP_ID, reads_all)
    week_start = date.today() - timedelta(days=date.today().weekday())
    count = db.birthdays_recorded_since(week_start.isoformat())
    parts.append(f"BIRTHDAYS ONLY\n{'🎂' if sees else '⚠️'} {line} · "
                 f"birthday wishes recorded this week: {count}")
  else:
    parts.append("BIRTHDAYS ONLY\nFELICITATION_GROUP_ID isn't set in .env, "
                 "so the brief has no birthdays.")
  never = [name for name, g in (("Leadership", LEADERSHIP_GROUP_ID),
                                ("Onboarding", ONBOARDING_GROUP_ID),
                                ("Induction", INDUCTION_GROUP_ID)) if g]
  parts.append("NEVER READ\n" + ", ".join(never) + (
    "\n" + "\n".join(off) if off else ""))
  parts.append("\"Stored today\" counts since midnight UTC; each day is "
               "summarised and deleted just after midnight.")
  await update.message.reply_text("\n\n".join(parts))
