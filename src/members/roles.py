"""Member roles: ordinary members, and admins.

Admins will be able to reach resources ordinary members can't. Gate a
future feature with the ADMINS filter (for commands and messages) or
is_admin() (inside a callback):

  app.add_handler(CommandHandler("resources", show_resources,
                                 filters=roles.ADMINS & filters.ChatType.PRIVATE))

The owner (OWNER_USER_ID) is above roles: always passes, can't be demoted,
and is the only person who can grant or remove a role by hand.

Anyone in the leadership group (LEADERSHIP_GROUP_ID) is automatically an
admin: made one when they join (or, for people already there, the first time
they post), and no longer one when they leave. Alpha must be an admin in that
group to see this. A role the owner granted by hand outranks it and stays. Owner-only commands,
in a private chat with Alpha:

  /admin                 show the "Pick admins" button
  /admin @name @name2    make existing members admins
  /admins                list the admins
  /unadmin @name         back to ordinary member
"""
import logging

from telegram import (KeyboardButton, KeyboardButtonRequestUsers,
                      ReplyKeyboardMarkup, ReplyKeyboardRemove)
from telegram.ext import ApplicationHandlerStop, ContextTypes, filters

from src import db
from src.common.telegram_helpers import UsersPicked, handle_or_name
from src.config import LEADERSHIP_GROUP_ID, OWNER_USER_ID
from telegram.constants import ChatMemberStatus

logger = logging.getLogger(__name__)
# ===========================================================================

PICK_REQUEST_ID = 4202
PICKED = UsersPicked(PICK_REQUEST_ID)

HELP = ("🛡 Admins will be able to reach resources ordinary members can't.\n\n"
        "Tap the button below to pick people from your chats.\n\n"
        "Or send /admin @username for people already on record.\n\n"
        "/admins lists them. /unadmin @username makes someone an ordinary "
        "member again.")


def is_owner(user_id):
  return bool(OWNER_USER_ID) and user_id == OWNER_USER_ID


def is_admin(user_id):
  """Owner, or a member holding the admin role."""
  return is_owner(user_id) or db.get_role(user_id) == db.ADMIN


class _IsAdmin(filters.MessageFilter):
  def filter(self, message):
    user = message.from_user
    return user is not None and is_admin(user.id)


ADMINS = _IsAdmin(name="ADMINS")


def _picker():
  return ReplyKeyboardMarkup([[KeyboardButton(
    "🛡 Pick admins",
    request_users=KeyboardButtonRequestUsers(
      request_id=PICK_REQUEST_ID, user_is_bot=False, max_quantity=10,
      request_name=True, request_username=True))]],
    resize_keyboard=True, one_time_keyboard=True)


def _summary(made, already, owner_skipped, unknown=(), bad=(), kept=()):
  lines = []
  if made:
    lines.append("🛡 Now admins: " + ", ".join(made))
  if kept:
    lines.append("Already admins through the leadership group; now they stay "
                 "admins even if they leave it: " + ", ".join(kept))
  if already:
    lines.append("Already admins: " + ", ".join(already))
  if owner_skipped:
    lines.append("You're the owner: you already have every permission.")
  if unknown:
    lines.append("Not on record yet, so I can't link them: " + ", ".join(unknown)
                 + ". Use the picker, or ask them to message Alpha first.")
  if bad:
    lines.append("Not valid usernames: " + ", ".join(bad))
  return "\n\n".join(lines) or "Nobody was picked."


async def cmd_admin(update, context: ContextTypes.DEFAULT_TYPE):
  owner = update.effective_user.id
  names = [a for a in (context.args or []) if a.strip()]
  if not names:
    await update.message.reply_text(HELP, reply_markup=_picker())
    return
  made, already, kept, unknown, bad, owner_skipped = [], [], [], [], [], False
  for raw in names:
    key = db.username_key(raw)
    if key is None:
      bad.append(raw)
      continue
    known = db.find_member_by_username(key)
    if known is None:
      unknown.append(f"@{key}")
    elif is_owner(known["user_id"]):
      owner_skipped = True
    else:
      was = db.get_role_source(known["user_id"])
      if db.grant_role(known["user_id"], known["username"], known["first_name"],
                       known["last_name"], db.ADMIN, owner):
        made.append(f"@{key}")
      elif was == db.FROM_LEADERSHIP:
        kept.append(f"@{key}")
      else:
        already.append(f"@{key}")
  await update.message.reply_text(
    _summary(made, already, owner_skipped, unknown, bad, kept))


async def handle_picked(update, context: ContextTypes.DEFAULT_TYPE):
  """Group -1: the owner picked admins. Telegram gives Alpha their ids."""
  owner = update.effective_user.id
  made, already, kept, owner_skipped = [], [], [], False
  for u in update.effective_message.users_shared.users:
    name = f"@{u.username}" if u.username else (u.first_name or "someone")
    if is_owner(u.user_id):
      owner_skipped = True
      continue
    was = db.get_role_source(u.user_id)
    if db.grant_role(u.user_id, u.username, u.first_name, u.last_name, db.ADMIN, owner):
      made.append(name)
    elif was == db.FROM_LEADERSHIP:
      kept.append(name)
    else:
      already.append(name)
  await update.effective_message.reply_text(
    _summary(made, already, owner_skipped, kept=kept), reply_markup=ReplyKeyboardRemove())
  raise ApplicationHandlerStop


async def cmd_admins(update, context: ContextTypes.DEFAULT_TYPE):
  rows = db.members_with_role(db.ADMIN)
  lines = [f"🛡 ADMINS ({len(rows)})", ""]
  for r in rows:
    origin = "leadership group" if r["source"] == db.FROM_LEADERSHIP else "added by you"
    flag = "" if r["status"] == db.STATUS_ACTIVE else f", {r['status']}"
    lines.append(f"• {handle_or_name(r)}  ({origin}{flag})")
  if not rows:
    lines.append("None yet. Send /admin to add some.")
  if not LEADERSHIP_GROUP_ID:
    lines += ["", "Leadership-group admins are off: LEADERSHIP_GROUP_ID isn't set."]
  await update.message.reply_text("\n".join(lines))


async def cmd_unadmin(update, context: ContextTypes.DEFAULT_TYPE):
  owner = update.effective_user.id
  names = [a for a in (context.args or []) if a.strip()]
  if not names:
    await update.message.reply_text("Send /unadmin @username")
    return
  done, missing, leaders = [], [], []
  for raw in names:
    key = db.username_key(raw)
    known = db.find_member_by_username(key) if key else None
    if known is not None and db.get_role_source(known["user_id"]) == db.FROM_LEADERSHIP:
      leaders.append(f"@{key}")   # removing it here wouldn't stick
    elif known is not None and db.revoke_role(known["user_id"], owner):
      done.append(f"@{key}")
    else:
      missing.append(raw)
  lines = []
  if done:
    lines.append("Now ordinary members: " + ", ".join(done))
  if leaders:
    lines.append("Admins because they're in the leadership group: " + ", ".join(leaders)
                 + ". Remove them from that group and their admin role goes with it.")
  if missing:
    lines.append("Not admins: " + ", ".join(missing))
  await update.message.reply_text("\n\n".join(lines))


# ----- The leadership group makes admins ----------------------------------------

_SEEN = "leadership_seen"   # bot_data: ids already checked this run


def _in_group(chat_member):
  if chat_member.status in (ChatMemberStatus.OWNER, ChatMemberStatus.ADMINISTRATOR,
                            ChatMemberStatus.MEMBER):
    return True
  return (chat_member.status == ChatMemberStatus.RESTRICTED
          and getattr(chat_member, "is_member", False))


def _make_leader_admin(user):
  if user.is_bot or is_owner(user.id):
    return False
  return db.grant_role(user.id, user.username, user.first_name, user.last_name,
                       db.ADMIN, None, source=db.FROM_LEADERSHIP)


async def handle_leadership_post(update, context: ContextTypes.DEFAULT_TYPE):
  """Any post in the leadership group: Telegram can't list a group's members,
  so people who were already there are recognised the first time they post.
  Reads only who posted; nothing they write is stored."""
  user = update.effective_user
  if user is None:
    return
  seen = context.bot_data.setdefault(_SEEN, set())
  if user.id in seen:
    return
  seen.add(user.id)
  if _make_leader_admin(user):
    logger.info("Leadership member made admin on first post")


async def handle_leadership_member_update(update, context: ContextTypes.DEFAULT_TYPE):
  """Joins and leaves in the leadership group."""
  change = update.chat_member
  if change is None or change.chat.id != LEADERSHIP_GROUP_ID:
    return
  user = change.new_chat_member.user
  was, now = _in_group(change.old_chat_member), _in_group(change.new_chat_member)
  if not was and now:
    if _make_leader_admin(user):
      logger.info("Leadership member made admin on joining")
  elif was and not now:
    by = change.from_user.id if change.from_user else None
    if db.revoke_role(user.id, by, only_source=db.FROM_LEADERSHIP):
      logger.info("Admin role removed: left the leadership group")
    context.bot_data.setdefault(_SEEN, set()).discard(user.id)
