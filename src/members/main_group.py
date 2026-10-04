"""Keeps the members database and the main ATL group in step.

Rule: everyone in the main group must be on record, and every approved
member must be in the main group. The Bot API can't list a group's members,
so this is enforced whenever Alpha sees someone: a message to Alpha, a post
in the main group (legacy.passive_link), or someone joining or leaving.

The owner (OWNER_USER_ID) is the only person who may add members without
onboarding. Anyone else who gets in without a record is removed, and the
owner is told how they got in.
"""
import logging
import time
from datetime import datetime, timedelta, timezone

from telegram.constants import ChatMemberStatus
from telegram.error import Forbidden, TelegramError
from telegram.ext import ContextTypes

from src import db
from src.common.telegram_helpers import handle_or_name
from src.members import special
from src.config import (INVITE_TTL_SECONDS, MAIN_GROUP_ID, ONBOARDING_ENTRY_URL,
                        OWNER_USER_ID)

logger = logging.getLogger(__name__)
# ===========================================================================

IN, OUT, BANNED, UNKNOWN = "in", "out", "banned", "unknown"
CACHE_SECONDS = 10 * 60   # don't ask Telegram on every single message

_cache = {}   # user_id -> (state, when)

# ----- Messages -------------------------------------------------------------
REMOVED_TEXT = ("Your ATL membership has been removed. If you think this is a "
                "mistake, please contact an ATL admin.")
REJOIN_TEXT = ("You're not in the main ATL group at the moment. Here's your "
               "personal link to rejoin. It works once and expires in "
               f"{INVITE_TTL_SECONDS // 3600} hours.")


# ----- Is this person in the main group? ------------------------------------

def _state_of(chat_member):
  status = chat_member.status
  if status in (ChatMemberStatus.OWNER, ChatMemberStatus.ADMINISTRATOR,
                ChatMemberStatus.MEMBER):
    return IN
  if status == ChatMemberStatus.RESTRICTED:
    return IN if getattr(chat_member, "is_member", False) else OUT
  if status == ChatMemberStatus.BANNED:
    return BANNED
  return OUT   # left


async def main_group_state(bot, user_id, use_cache=True):
  """IN, OUT, BANNED, or UNKNOWN if Telegram couldn't be asked. Callers
  must treat UNKNOWN as 'do nothing': never remove or invite on a guess."""
  if not MAIN_GROUP_ID:
    return UNKNOWN
  hit = _cache.get(user_id)
  if use_cache and hit and time.monotonic() - hit[1] < CACHE_SECONDS:
    return hit[0]
  try:
    member = await bot.get_chat_member(MAIN_GROUP_ID, user_id)
  except TelegramError:
    logger.warning("Could not check main-group membership", exc_info=True)
    return UNKNOWN
  state = _state_of(member)
  _cache[user_id] = (state, time.monotonic())
  return state


async def in_main_group(bot, user_id):
  """Asks Telegram directly, never the cache: for moments that decide
  something, like recognising someone as a member. A DM proves nothing
  about membership."""
  return await main_group_state(bot, user_id, use_cache=False) == IN


# ----- Invite links -----------------------------------------------------------

async def _personal_invite(bot, user_id):
  link = await bot.create_chat_invite_link(
    chat_id=MAIN_GROUP_ID,
    name=f"for {handle_or_name(db.get_member(user_id))}"[:32],   # no ids in admin view
    member_limit=1,
    expire_date=datetime.now(timezone.utc)
                + timedelta(seconds=INVITE_TTL_SECONDS),
  )
  return link.invite_link


async def invite_for(bot, user_id):
  """A one-use invite to the main group, re-sending a still-valid one rather
  than minting a new link on every message."""
  link = db.get_invite(user_id)
  if link is None:
    link = await _personal_invite(bot, user_id)
    db.save_invite(user_id, link, INVITE_TTL_SECONDS)
  return link


# ----- Join requests -------------------------------------------------------
# Not currently used: member_limit=1 links never raise join requests.
# Kept as a safety net if the invite strategy ever changes.
async def handle_join_request(update, context):
  req = update.chat_join_request
  user_id = req.from_user.id
  member = db.get_member(user_id)

  if member and member["status"] == db.STATUS_ACTIVE:
    await context.bot.approve_chat_join_request(req.chat.id, user_id)
    db.log_event(user_id, "joined_main_group")
  else:
    await context.bot.decline_chat_join_request(req.chat.id, user_id)
    if member is not None:   # the audit table needs a member row to point at
      db.log_event(user_id, "join_request_declined")


# ----- Someone joins or leaves the main group -------------------------------

def _how_they_got_in(update, user):
  by = update.from_user
  link = update.invite_link
  if link is not None:
    creator = link.creator.full_name if link.creator else "someone"
    return f"joined through an invite link created by {creator}"
  if update.via_join_request:
    return f"join request approved by {by.full_name}"
  if by is not None and by.id != user.id:
    return f"added by {by.full_name}"
  return "joined directly (a public link or the group's username)"


def _owner_let_them_in(update):
  if not OWNER_USER_ID:
    return False
  if update.from_user is not None and update.from_user.id == OWNER_USER_ID:
    return True   # the owner added them, or approved their request
  link = update.invite_link
  return bool(link and link.creator and link.creator.id == OWNER_USER_ID)


def _removal_notice():
  where = (f"Please start here: {ONBOARDING_ENTRY_URL}" if ONBOARDING_ENTRY_URL
           else "Please contact an ATL admin to begin.")
  return ("You've been removed from the ATL main group because you joined "
          "without going through ATL's onboarding process.\n\n"
          f"Everyone joins through onboarding first. {where}")


async def _alert_owner(bot, text):
  if not OWNER_USER_ID:
    return
  try:
    await bot.send_message(OWNER_USER_ID, text)
  except TelegramError:
    logger.warning("Could not alert the owner", exc_info=True)


async def _on_join(bot, update, user):
  db.clear_invite(user.id)   # whatever link they had is spent
  if special.claim(user):
    return   # a waiting Legacy Member, now in the main group: welcome
  member = db.get_member(user.id)
  if member is not None and member["status"] == db.STATUS_ACTIVE:
    return   # came through onboarding, or already recognised

  if _owner_let_them_in(update):
    db.activate_by_owner(user.id, user.username, user.first_name, user.last_name)
    logger.info("Owner added a member directly; recorded with an empty profile")
    return

  if not OWNER_USER_ID:
    logger.warning("OWNER_USER_ID is not set, so an unrecorded join was NOT "
                   "reversed. Set it in .env to enforce onboarding.")
    return

  how = _how_they_got_in(update, user)
  try:
    await bot.ban_chat_member(MAIN_GROUP_ID, user.id)
    await bot.unban_chat_member(MAIN_GROUP_ID, user.id, only_if_banned=True)
    removed = "✅ Removed from the group. They can rejoin through onboarding."
  except TelegramError as e:
    removed = (f"❌ Could NOT remove them: {e}\n"
               "Alpha needs the 'Ban users' admin right in the main group.")

  told = "✉️ Couldn't message them: they've never started Alpha, so Telegram won't allow it."
  if removed.startswith("✅"):
    try:
      await bot.send_message(user.id, _removal_notice())
      told = "✉️ They were told why and where to start."
    except Forbidden:
      pass
    except TelegramError:
      logger.warning("Could not tell a removed joiner why", exc_info=True)

  handle = f"@{user.username}" if user.username else "(no username)"
  await _alert_owner(bot, (
    "⚠️ Someone joined the ATL main group without going through onboarding.\n\n"
    f"Name: {user.full_name}\nUsername: {handle}\nUser ID: {user.id}\n"
    f"How: {how}\n\n{removed}\n{told}"))


def _on_leave(update, user, now):
  member = db.get_member(user.id)
  if member is None or member["status"] != db.STATUS_ACTIVE:
    return
  if now == BANNED:
    # Removal for cause is ATL's only exit: both lists must agree they're out,
    # or Alpha would hand them a fresh invite next time they message.
    by = update.from_user
    db.set_status(user.id, db.STATUS_REMOVED,
                  actor_user_id=by.id if by else None,
                  note="removed from the main group in Telegram")
  # Left on their own: membership is permanent, so they stay active and
  # Alpha offers a new invite when they next message.


async def handle_main_group_member_update(update, context: ContextTypes.DEFAULT_TYPE):
  """Every join, leave and removal in the main group (Alpha must be an admin
  there to receive these)."""
  change = update.chat_member
  if change is None or change.chat.id != MAIN_GROUP_ID:
    return
  user = change.new_chat_member.user
  if user.is_bot:
    return
  before, now = _state_of(change.old_chat_member), _state_of(change.new_chat_member)
  _cache[user.id] = (now, time.monotonic())
  if before != IN and now == IN:
    await _on_join(context.bot, change, user)
  elif before == IN and now != IN:
    _on_leave(change, user, now)
