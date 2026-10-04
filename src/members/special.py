"""The owner's special list: "Legacy Members", active without onboarding.

Owner-only commands, in a private chat with Alpha:
  /legacy                 show the "Pick Legacy Members" button
  /legacy @name @name2    add by username
  /legacylist             who's on the list, and who's still waiting
  /legacyremove @name     take someone off the list (their status stays)

A bot can't turn a username into a Telegram id. Picking people from your
chats gives Alpha the id at once. A typed username waits until that person
messages Alpha, posts in the main group, or joins it (db.claim_special,
called from legacy.passive_link and main_group).

"Legacy Member" is what people see. In the code this is special_members,
so it can't be confused with legacy_members (the old website's backlog).
"""
import logging

from telegram import (KeyboardButton, KeyboardButtonRequestUsers,
                      ReplyKeyboardMarkup, ReplyKeyboardRemove)
from telegram.ext import ApplicationHandlerStop, ContextTypes

from src import db
from src.common.telegram_helpers import UsersPicked, handle_or_name

logger = logging.getLogger(__name__)
# ===========================================================================

PICK_REQUEST_ID = 4201   # identifies our picker's answer
PICKED = UsersPicked(PICK_REQUEST_ID)
TAG = "⭐ Legacy Member"

HELP = ("⭐ Legacy Members are active in ATL without going through onboarding.\n\n"
        "Tap the button below to pick people from your chats: they're added "
        "straight away.\n\nOr send /legacy @username (several at once is fine). "
        "Alpha links each one the first time they message Alpha, post in the "
        "main group, or join it.\n\n/legacylist shows the list. "
        "/legacyremove @username takes someone off it.")


def _picker():
  return ReplyKeyboardMarkup([[KeyboardButton(
    "⭐ Pick Legacy Members",
    request_users=KeyboardButtonRequestUsers(
      request_id=PICK_REQUEST_ID, user_is_bot=False, max_quantity=10,
      request_name=True, request_username=True))]],
    resize_keyboard=True, one_time_keyboard=True)


async def cmd_legacy(update, context: ContextTypes.DEFAULT_TYPE):
  owner = update.effective_user.id
  names = [a for a in (context.args or []) if a.strip()]
  if not names:
    await update.message.reply_text(HELP, reply_markup=_picker())
    return

  now, waiting, already, bad = [], [], [], []
  for raw in names:
    key = db.username_key(raw)
    if key is None:
      bad.append(raw)
      continue
    known = db.find_member_by_username(key)
    if known is not None:
      added = db.add_special(known["user_id"], known["username"], known["first_name"],
                             known["last_name"], key, owner)
      (now if added else already).append(f"@{key}")
    elif db.add_special_waiting(key, owner):
      waiting.append(f"@{key}")
    else:
      already.append(f"@{key} (waiting)")

  lines = []
  if now:
    lines.append("✅ Added and active now: " + ", ".join(now))
  if waiting:
    lines.append("⏳ Added; Alpha will link them the first time they message "
                 "Alpha, post in the main group, or join it: " + ", ".join(waiting))
  if already:
    lines.append("Already on the list: " + ", ".join(already))
  if bad:
    lines.append("Not valid usernames: " + ", ".join(bad))
  await update.message.reply_text("\n\n".join(lines))


async def handle_picked(update, context: ContextTypes.DEFAULT_TYPE):
  """Group -1: the owner picked people with the button. Telegram gives Alpha
  their ids directly, so they're linked and active at once."""
  shared = update.effective_message.users_shared
  owner = update.effective_user.id
  added, already = [], []
  for u in shared.users:
    key = db.username_key(u.username) if u.username else None
    ok = db.add_special(u.user_id, u.username, u.first_name, u.last_name, key, owner)
    name = f"@{u.username}" if u.username else (u.first_name or "someone")
    (added if ok else already).append(name)
  lines = []
  if added:
    lines.append("✅ Added as Legacy Members, active now: " + ", ".join(added))
  if already:
    lines.append("Already on the list: " + ", ".join(already))
  await update.effective_message.reply_text(
    "\n\n".join(lines) or "Nobody was picked.", reply_markup=ReplyKeyboardRemove())
  raise ApplicationHandlerStop


async def cmd_legacylist(update, context: ContextTypes.DEFAULT_TYPE):
  linked, waiting = db.list_specials()
  lines = [f"⭐ LEGACY MEMBERS ({len(linked)} linked, {len(waiting)} waiting)", ""]
  for r in linked:
    flag = "" if r["status"] == db.STATUS_ACTIVE else f"  ({r['status']})"
    lines.append(f"• {handle_or_name(r)}{flag}")
  if waiting:
    lines += ["", "Waiting to be seen:"] + [f"• @{k}" for k in waiting]
  if not linked and not waiting:
    lines.append("Nobody yet. Send /legacy to add people.")
  await update.message.reply_text("\n".join(lines))


async def cmd_legacyremove(update, context: ContextTypes.DEFAULT_TYPE):
  names = [a for a in (context.args or []) if a.strip()]
  if not names:
    await update.message.reply_text("Send /legacyremove @username")
    return
  removed, missing = [], []
  for raw in names:
    key = db.username_key(raw)
    if key and db.remove_special(key):
      removed.append(f"@{key}")
    else:
      missing.append(raw)
  lines = []
  if removed:
    lines.append("Removed from the list (their membership is unchanged): "
                 + ", ".join(removed))
  if missing:
    lines.append("Not on the list: " + ", ".join(missing))
  await update.message.reply_text("\n\n".join(lines))


def claim(user):
  """Alpha has just seen this Telegram user. True if a waiting entry for
  their username was waiting for them (they're now linked and active)."""
  if user is None or user.is_bot or not user.username:
    return False
  claimed = db.claim_special(user.id, user.username, user.first_name,
                             user.last_name, db.username_key(user.username))
  if claimed:
    logger.info("Legacy Member linked on first sight")
  return claimed
