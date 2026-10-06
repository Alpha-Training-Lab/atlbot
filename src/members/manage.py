"""Looking members up and expelling them, from a private chat with Alpha.

Who can do what:
  owner                everything below, plus /reinstate and /obtlead
  managers (/obtlead)  /member and /expel, for ordinary members only

Managers are a permission of their own, not a role: the leadership group
makes people admins automatically, and that must never hand out access to
members' data.

  /member @username      a member's full record, with their ID photos
  /member some name      search by name; tap a result to open it
  /expel @username       expel, after choosing a reason and confirming
  /reinstate @username   owner only: undo an expulsion
  /obtlead               owner only: list managers; /obtlead @name adds one
  /unobtlead @name       owner only

Every lookup and expulsion is written to the audit trail with who did it.
Records shown here delete themselves after a few minutes.
"""
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import Forbidden, TelegramError
from telegram.ext import ContextTypes, filters

from src import db
from src.common.telegram_helpers import handle_or_name, send_file, who
from src.config import (INDUCTION_GROUP_ID, LEADERSHIP_GROUP_ID, MAIN_GROUP_ID,
                        OWNER_USER_ID)
from src.kyc_form import active_fields, display_value, label

logger = logging.getLogger(__name__)
# ===========================================================================

VIEW_DELETE_SECONDS = 10 * 60   # a record holds personal data and ID photos

EXPEL_REASONS = [
  "Didn't complete their records",
  "Broke the community rules",
  "False or fake details",
  "Scam or fraud",
  "Inactive member",
]

HELP = ("🔎 /member @username shows a member's record and ID photos.\n"
        "/member <name> searches by name.\n"
        "/expel @username removes a member from ATL, after you pick a reason "
        "and confirm.")


# ----- Permissions ------------------------------------------------------------

def is_owner(user_id):
  return bool(OWNER_USER_ID) and user_id == OWNER_USER_ID


def can_manage(user_id):
  return is_owner(user_id) or db.is_manager(user_id)


class _CanManage(filters.MessageFilter):
  def filter(self, message):
    user = message.from_user
    return user is not None and can_manage(user.id)


MANAGERS = _CanManage(name="MANAGERS")


def _may_expel(actor_id, target_id):
  """(allowed, why_not). Nobody expels the owner; only the owner expels
  someone holding a role, manager access, or a Legacy Member place."""
  if is_owner(target_id):
    return False, "The owner can't be expelled."
  if actor_id == target_id:
    return False, "You can't expel yourself."
  if is_owner(actor_id):
    return True, None
  s = db.member_summary(target_id)
  if s["role"] or s["manager"] or s["legacy_member"]:
    return False, ("This member is an admin, an onboarding lead or a Legacy "
                   "Member, so only the owner can expel them.")
  return True, None


# ----- Finding a member -----------------------------------------------------------

def _find(args):
  """(member, matches): one member for an @username, or name matches."""
  text = " ".join(args).strip()
  key = db.username_key(text) if text.startswith("@") or " " not in text else None
  if key:
    member = db.find_member_by_username(key)
    if member is not None:
      return member, []
  if text.startswith("@"):
    return None, []
  return None, db.search_members(text) if len(text) >= 3 else []


def _record_text(member):
  user_id = member["user_id"]
  s = db.member_summary(user_id)
  answers = db.get_kyc_answers(user_id)
  pending = {c["field_key"] for c in db.get_open_changes(user_id)}

  tags = [f"Status: {member['status']}"]
  if s["role"]:
    tags.append(f"Role: {s['role']}")
  if s["manager"]:
    tags.append("Onboarding lead")
  if is_owner(user_id):
    tags.append("Owner")
  lines = ["🔎 MEMBER RECORD", "", f"Telegram: {who(member)}", " · ".join(tags)]
  if s["legacy_member"]:
    lines.append("⭐ Legacy Member")
  if s["expelled"]:
    lines.append(f"🚫 Expelled {s['expelled']['created_at'][:10]}: "
                 f"{s['expelled']['note'] or 'no reason given'}")
  lines.append("")

  for field in active_fields():
    row = answers.get(field["key"])
    if field["type"] == "document":
      value = "📎 below" if row and row["file_ref"] else "— missing"
    elif row is None:
      value = "— missing"
    else:
      value = display_value(field, row["value_text"])
    flags = ""
    if row is not None and row["needs_review"]:
      flags += "  ⚠️ needs checking"
    if field["key"] in pending:
      flags += "  ⏳ change awaiting approval"
    lines.append(f"{label(field)}: {value}{flags}")

  lines += ["", f"This record deletes itself in {VIEW_DELETE_SECONDS // 60} minutes."]
  return "\n".join(lines)


async def _show_record(bot, chat_id, actor_id, member):
  user_id = member["user_id"]
  allowed, _ = _may_expel(actor_id, user_id)
  markup = None
  if allowed and member["status"] != db.STATUS_REMOVED:
    markup = InlineKeyboardMarkup([[
      InlineKeyboardButton("🚫 Expel from ATL", callback_data=f"mm:x:{user_id}")]])
  sent = [await bot.send_message(chat_id, _record_text(member), reply_markup=markup)]

  answers = db.get_kyc_answers(user_id)
  for field in active_fields():
    row = answers.get(field["key"])
    if field["type"] == "document" and row and row["file_ref"]:
      try:
        sent.append(await send_file(bot, chat_id, row["file_ref"],
                                    caption=f"{label(field)}, {handle_or_name(member)}",
                                    reply_to=sent[0].message_id))
      except TelegramError:
        logger.warning("Could not send a stored ID photo", exc_info=True)

  for m in sent:
    db.schedule_deletion(chat_id, m.message_id, VIEW_DELETE_SECONDS)
  db.log_event(user_id, "member_viewed", actor_user_id=actor_id)


async def cmd_member(update, context: ContextTypes.DEFAULT_TYPE):
  actor = update.effective_user.id
  if not context.args:
    await update.message.reply_text(HELP)
    return
  member, matches = _find(context.args)
  if member is not None:
    await _show_record(context.bot, update.effective_chat.id, actor, member)
    return
  if not matches:
    await update.message.reply_text(
      "No member found. Check the username, or search by name: /member Ada Okafor")
    return
  rows = [[InlineKeyboardButton(f"{handle_or_name(m)}  ({m['status']})",
                                callback_data=f"mm:v:{m['user_id']}")] for m in matches]
  await update.message.reply_text(
    f"{len(matches)} match{'es' if len(matches) != 1 else ''}. Tap one to open it:",
    reply_markup=InlineKeyboardMarkup(rows))


# ----- Expelling --------------------------------------------------------------------

async def cmd_expel(update, context: ContextTypes.DEFAULT_TYPE):
  actor = update.effective_user.id
  if not context.args:
    await update.message.reply_text("Send /expel @username")
    return
  member, _ = _find(context.args[:1])
  if member is None:
    await update.message.reply_text("No member with that username. Try /member <name>.")
    return
  if member["status"] == db.STATUS_REMOVED:
    await update.message.reply_text(f"{handle_or_name(member)} is already expelled.")
    return
  allowed, why = _may_expel(actor, member["user_id"])
  if not allowed:
    await update.message.reply_text(why)
    return
  await update.message.reply_text(**_reason_prompt(member))


def _reason_prompt(member):
  uid = member["user_id"]
  rows = [[InlineKeyboardButton(r, callback_data=f"mm:r:{uid}:{i}")]
          for i, r in enumerate(EXPEL_REASONS)]
  rows.append([InlineKeyboardButton("✖ Cancel", callback_data="mm:n")])
  return {"text": f"Why is {handle_or_name(member)} being expelled?",
          "reply_markup": InlineKeyboardMarkup(rows)}


async def _ban_everywhere(bot, user_id):
  """Ban from every ATL group Alpha manages, so no old link lets them back
  in. Returns the groups it couldn't ban them from."""
  failed = []
  for name, chat_id in (("main group", MAIN_GROUP_ID),
                        ("induction group", INDUCTION_GROUP_ID),
                        ("leadership group", LEADERSHIP_GROUP_ID)):
    if not chat_id:
      continue
    try:
      await bot.ban_chat_member(chat_id, user_id)
    except TelegramError as e:
      logger.warning("Could not ban from the %s: %s", name, e)
      failed.append(name)
  return failed


async def _expel(bot, actor, member, reason):
  user_id = member["user_id"]
  if not db.expel_member(user_id, actor.id, reason):
    return f"{handle_or_name(member)} was already expelled."
  failed = await _ban_everywhere(bot, user_id)

  told = "They were told by DM."
  try:
    await bot.send_message(
      user_id, "You have been removed from Alpha Training Lab.\n\n"
               f"Reason: {reason}\n\n"
               "If you believe this is a mistake, please contact an ATL admin.")
  except Forbidden:
    told = "Couldn't message them (they've never started Alpha)."
  except TelegramError:
    told = "Couldn't message them."

  lines = [f"🚫 {handle_or_name(member)} has been expelled.", f"Reason: {reason}", told]
  if failed:
    lines.append("⚠️ Couldn't remove them from the " + ", ".join(failed)
                 + ". Alpha needs the 'Ban users' admin right there; remove them by hand.")
  report = "\n".join(lines)

  if not is_owner(actor.id) and OWNER_USER_ID:
    try:
      await bot.send_message(OWNER_USER_ID, f"{report}\nBy: {actor.full_name}")
    except TelegramError:
      logger.warning("Could not tell the owner about an expulsion", exc_info=True)
  return report


async def handle_button(update, context: ContextTypes.DEFAULT_TYPE):
  """mm:v:<uid> open, mm:x:<uid> expel, mm:r:<uid>:<i> reason,
  mm:y:<uid>:<i> confirm, mm:n cancel."""
  query = update.callback_query
  actor = query.from_user
  if not can_manage(actor.id) or query.message.chat.type != "private":
    await query.answer("Only the owner and onboarding leads can do this.", show_alert=True)
    return
  await query.answer()
  parts = (query.data or "").split(":")
  action = parts[1] if len(parts) > 1 else ""

  if action == "n":
    await query.edit_message_text("Cancelled. Nobody was expelled.")
    return
  try:
    member = db.get_member(int(parts[2]))
    reason_i = int(parts[3]) if len(parts) > 3 else None
  except (IndexError, ValueError):
    return
  if member is None:
    await query.edit_message_text("That member is no longer on record.")
    return

  if action == "v":
    await query.edit_message_reply_markup(reply_markup=None)
    await _show_record(context.bot, query.message.chat.id, actor.id, member)
    return

  allowed, why = _may_expel(actor.id, member["user_id"])
  if not allowed:
    await query.edit_message_text(why)
    return
  if action == "x":
    await context.bot.send_message(query.message.chat.id, **_reason_prompt(member))
    return
  if reason_i is None or not 0 <= reason_i < len(EXPEL_REASONS):
    return
  reason = EXPEL_REASONS[reason_i]
  if action == "r":
    await query.edit_message_text(
      f"Expel {handle_or_name(member)} from ATL?\n\nReason: {reason}\n\n"
      "They'll be removed from the ATL groups Alpha manages and told why.",
      reply_markup=InlineKeyboardMarkup([[
        InlineKeyboardButton("🚫 Yes, expel", callback_data=f"mm:y:{member['user_id']}:{reason_i}"),
        InlineKeyboardButton("✖ Cancel", callback_data="mm:n"),
      ]]))
    return
  if action == "y":
    await query.edit_message_text("Expelling…")
    await query.edit_message_text(await _expel(context.bot, actor, member, reason))


# ----- Owner only -----------------------------------------------------------------

async def cmd_reinstate(update, context: ContextTypes.DEFAULT_TYPE):
  if not context.args:
    await update.message.reply_text("Send /reinstate @username")
    return
  member, _ = _find(context.args[:1])
  restored = (db.reinstate_member(member["user_id"], update.effective_user.id)
              if member is not None else None)
  if restored is None:
    await update.message.reply_text("Nobody expelled with that username.")
    return
  for chat_id in (MAIN_GROUP_ID, INDUCTION_GROUP_ID, LEADERSHIP_GROUP_ID):
    if chat_id:
      try:
        await context.bot.unban_chat_member(chat_id, member["user_id"], only_if_banned=True)
      except TelegramError:
        logger.warning("Could not unban a reinstated member", exc_info=True)

  if restored == db.STATUS_ACTIVE:
    to_them = ("Your ATL membership has been restored. Message me any time "
               "and I'll send your link back into the main group.")
    to_owner = (f"✅ {handle_or_name(member)} is an active member again and unbanned. "
                "When they message Alpha they'll get a link back into the main group.")
  else:
    to_them = ("You're welcome to apply to ATL again. Please start in the "
               "induction group and follow the steps there.")
    to_owner = (f"✅ {handle_or_name(member)} is unbanned. They were still an "
                "applicant when expelled, so they start onboarding again from the "
                "induction group: induction review, KYC and vouch as normal.")
  try:
    await context.bot.send_message(member["user_id"], to_them)
  except TelegramError:
    pass
  await update.message.reply_text(
    f"{to_owner} Roles, onboarding-lead access and Legacy Member status are not "
    "restored; add them again if needed.")


async def cmd_obtlead(update, context: ContextTypes.DEFAULT_TYPE):
  owner = update.effective_user.id
  if not context.args:
    rows = db.list_managers()
    lines = [f"🔎 ONBOARDING LEADS ({len(rows)})",
             "They can look members up (/member) and expel ordinary members (/expel).", ""]
    lines += [f"• {handle_or_name(r)}"
              + ("" if r["status"] == db.STATUS_ACTIVE else f" ({r['status']}: no access)")
              for r in rows] or ["None yet."]
    lines += ["", "Add one: /obtlead @username   Remove: /unobtlead @username"]
    await update.message.reply_text("\n".join(lines))
    return
  done, known_already, not_active, unknown = [], [], [], []
  for raw in context.args:
    key = db.username_key(raw)
    m = db.find_member_by_username(key) if key else None
    if m is None:
      unknown.append(raw)
    elif is_owner(m["user_id"]):
      continue
    elif m["status"] != db.STATUS_ACTIVE:
      not_active.append(f"@{key} ({m['status']})")
    elif db.grant_manager(m["user_id"], owner):
      done.append(f"@{key}")
    else:
      known_already.append(f"@{key}")
  lines = []
  if done:
    lines.append("🔎 Can now look members up and expel them: " + ", ".join(done))
  if known_already:
    lines.append("Already onboarding leads: " + ", ".join(known_already))
  if not_active:
    lines.append("Not active members, so not made leads: " + ", ".join(not_active))
  if unknown:
    lines.append("Not on record (ask them to message Alpha first): " + ", ".join(unknown))
  await update.message.reply_text("\n\n".join(lines) or "Nothing to change.")


async def cmd_unobtlead(update, context: ContextTypes.DEFAULT_TYPE):
  done, missing = [], []
  for raw in context.args or []:
    key = db.username_key(raw)
    m = db.find_member_by_username(key) if key else None
    if m is not None and db.revoke_manager(m["user_id"], update.effective_user.id):
      done.append(f"@{key}")
    else:
      missing.append(raw)
  lines = []
  if done:
    lines.append("No longer onboarding leads: " + ", ".join(done))
  if missing:
    lines.append("Not onboarding leads: " + ", ".join(missing))
  await update.message.reply_text("\n\n".join(lines) or "Send /unobtlead @username")
