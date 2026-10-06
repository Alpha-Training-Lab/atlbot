"""Step 1 of the weekly brief: store each text message posted in a group
the brief reads, and keep track of which groups those are.

main.py registers these in handler group -3, before every other handler.
Nothing here raises ApplicationHandlerStop, so every other handler still
sees every message.

Which groups are read:
  - the main group, from the moment this feature starts (seed_groups);
  - any group Alpha is added to later, if the person who added it is the
    owner or an admin;
  - never the groups in config.BRIEF_NEVER_READ.

The felicitation group is read for birthdays only (capture_birthday): no
text is stored, just who was wished a happy birthday.
"""
import logging
import re
from datetime import timezone

from telegram import MessageEntity, Update
from telegram.constants import ChatMemberStatus, ChatType
from telegram.ext import ContextTypes

from src import db
from src.config import BRIEF_NEVER_READ, FELICITATION_GROUP_ID, MAIN_GROUP_ID
from src.members import roles
# ===========================================================================
logger = logging.getLogger(__name__)

_IN_GROUP = {ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR}


def seed_groups():
  """Alpha joined the main group before this feature existed, so it never
  saw itself being added. Record it once; later changes are kept."""
  if MAIN_GROUP_ID and MAIN_GROUP_ID not in BRIEF_NEVER_READ:
    db.ensure_brief_group(MAIN_GROUP_ID, enabled=True)


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
  was_in = change.old_chat_member.status in _IN_GROUP
  now_in = change.new_chat_member.status in _IN_GROUP

  if now_in and not was_in:
    adder = change.from_user.id if change.from_user else None
    if chat_id == FELICITATION_GROUP_ID:
      enabled, why = False, "birthdays only"
    elif chat_id in BRIEF_NEVER_READ:
      enabled, why = False, "OFF (never read)"
    elif adder is not None and roles.is_admin(adder):
      enabled, why = True, "ON"
    else:
      enabled, why = False, "OFF (not added by an admin)"
    db.set_brief_group(chat_id, enabled, adder)
    logger.info("Brief: Alpha added to group %s, reading %s", chat_id, why)
  elif was_in and not now_in:
    db.remove_brief_group(chat_id)
    logger.info("Brief: Alpha left group %s", chat_id)
