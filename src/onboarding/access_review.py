"""Admin review of a finished registration: the card in the onboarding group,
approve (with a main-group invite) or decline (with reasons)."""
import logging
import re

from telegram import (
  ForceReply,
  InlineKeyboardButton,
  InlineKeyboardMarkup,
)
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from src import db
from src.common.telegram_helpers import send_file, who
from src.config import COOLDOWN_SECONDS, MAX_DECLINES, ONBOARDING_GROUP_ID
from src.kyc_form import active_fields, display_value, label
from src.members.main_group import invite_for
from src.onboarding.messages import welcome_approved
# ===========================================================================
logger = logging.getLogger(__name__)

DECLINE_REASONS = [
  "Document is blurry or unreadable",
  "Name does not match the ID",
  "The person in the photo does not match the ID",
  "Vouch could not be verified",
  "Answers incomplete or unclear",
]

_REASON_PROMPT = "DECLINE REASON for user {uid} (card {mid})"
_REASON_RE = re.compile(r"DECLINE REASON for user (\d+) \(card (\d+)\)")
_NOT_TOLD = ("⚠️ I couldn't message this member (they may have blocked me). "
             "Please contact them directly.")
# ===========================================================================
# --- card building ----------------------------------------------------
def build_kyc_card(user_id):
  member = db.get_member(user_id)
  answers = db.get_kyc_answers(user_id)

  lines = [
    "NEW REGISTRATION — awaiting access approval",
    "",
    f"Telegram: {who(member)}",
    f"User ID: {user_id}",
  ]

  declines = db.count_events(user_id, "status:declined")
  if declines:
    lines.append(f"⚠️ Previous declines: {declines} of {MAX_DECLINES}")

  lines.append("")

  for field in active_fields():
    row = answers.get(field["key"])
    if row is None:
      value = "— not answered —"
    elif row["file_ref"]:
      value = "📎 posted below"
    else:
      value = display_value(field, row["value_text"])
    flag = "  ⚠️ NEEDS REVIEW" if row and row["needs_review"] else ""
    lines.append(f"{label(field)}: {value}{flag}")

  return "\n".join(lines)


def _decision_keyboard(user_id):
  return InlineKeyboardMarkup([
    [
      InlineKeyboardButton("✅ Approve", callback_data=f"acc:approve:{user_id}"),
      InlineKeyboardButton("❌ Decline", callback_data=f"acc:decline:{user_id}"),
    ],
  ])


def _reason_keyboard(user_id, mask=0):
  rows = []
  for i, r in enumerate(DECLINE_REASONS):
    ticked = "☑️" if mask & (1 << i) else "▫️"
    rows.append([InlineKeyboardButton(
      f"{ticked} {r}", callback_data=f"acc:tog:{user_id}:{i}:{mask}")])
  rows.append([InlineKeyboardButton(
    "✏️ Other (type a reason)", callback_data=f"acc:oth:{user_id}")])
  if mask:
    rows.append([InlineKeyboardButton(
      "✅ Confirm decline", callback_data=f"acc:cfm:{user_id}:{mask}")])
  rows.append([InlineKeyboardButton(
    "← Back", callback_data=f"acc:back:{user_id}")])
  return InlineKeyboardMarkup(rows)


def _reasons_from_mask(mask):
  return [r for i, r in enumerate(DECLINE_REASONS) if mask & (1 << i)]


async def send_access_request(bot, user_id):
  card = await bot.send_message(
    chat_id=ONBOARDING_GROUP_ID,
    text=build_kyc_card(user_id),
    reply_markup=_decision_keyboard(user_id),
  )
  await post_documents(bot, user_id, card.message_id)


def _document_rows(user_id):
  answers = db.get_kyc_answers(user_id)
  return [(field, answers[field["key"]]) for field in active_fields()
          if field["key"] in answers and answers[field["key"]]["file_ref"]]


async def post_documents(bot, user_id, card_message_id):
  """Post the member's ID documents into the onboarding group, as replies
  to their card. Returns how many were posted."""
  rows = _document_rows(user_id)
  for field, row in rows:
    await send_file(bot, ONBOARDING_GROUP_ID, row["file_ref"],
                    caption=f"{label(field)}, user {user_id}",
                    reply_to=card_message_id)
  return len(rows)


async def _tell_member(bot, user_id, text, reply_markup=None, card_message_id=None):
  """Message the member; if Telegram refuses, tell the admins under the card
  instead of failing halfway through a decision."""
  try:
    await bot.send_message(chat_id=user_id, text=text, reply_markup=reply_markup)
    return True
  except TelegramError as e:
    logger.warning("Could not message member %s: %s", user_id, e)
    if card_message_id:
      await bot.send_message(ONBOARDING_GROUP_ID, _NOT_TOLD,
                             reply_to_message_id=card_message_id)
    return False


# --- decline ----------------------------------------------------------
async def _finalise_decline(bot, target_id, admin_id, admin_name, reason,
                            card_message_id=None, card_text=None):
  """Set status, tell the member, update the card. Returns False if
  the member was already decided."""
  member = db.get_member(target_id)
  if member is None or member["status"] != db.STATUS_PENDING_ACCESS:
    return False

  # Built before the status changes, so the decline count shown is the
  # previous ones, as on the original card.
  body = card_text or build_kyc_card(target_id)
  db.set_status(target_id, db.STATUS_DECLINED,
                actor_user_id=admin_id, note=reason)

  declines = db.count_events(target_id, "status:declined")
  at_cap = declines >= MAX_DECLINES

  if at_cap:
    text = ("Your ATL registration was not approved.\n\n"
            f"Reasons: {reason}\n\n"
            f"You have now used all {MAX_DECLINES} attempts. Please speak to "
            "an admin in the induction group before trying again — and note "
            f"there is a {COOLDOWN_SECONDS // 3600}-hour wait before any "
            "further submission.")
  else:
    # left = MAX_DECLINES - declines
    text = ("Your ATL registration was not approved.\n\n"
            f"Reason: {reason}\n\n"
            "You can fix this and submit again straight away. ")
            # f"You have {left} attempt{'s' if left > 1 else ''} left.")
  markup = InlineKeyboardMarkup([[
    InlineKeyboardButton("Start registration again", callback_data="rst:full")
  ]])

  await _tell_member(bot, target_id, text, markup, card_message_id)

  if card_message_id:
    await bot.edit_message_text(
      chat_id=ONBOARDING_GROUP_ID,
      message_id=card_message_id,
      text=f"{body}\n\n❌ DECLINED by {admin_name}\nReason: {reason}",
    )
  return True


# --- callbacks --------------------------------------------------------
async def handle_access_decision(update, context: ContextTypes.DEFAULT_TYPE):
  """acc:<action>:<user_id>[:...] on the registration card. Every path
  answers the button press exactly once: a second answer fails."""
  query = update.callback_query
  if query.message is None or query.message.chat.id != ONBOARDING_GROUP_ID:
    await query.answer()
    return

  parts = (query.data or "").split(":")
  try:
    action, target_id = parts[1], int(parts[2])
  except (IndexError, ValueError):
    logger.warning("Unparseable callback_data: %r", query.data)
    await query.answer()
    return

  admin_id = query.from_user.id
  admin_name = query.from_user.first_name or str(admin_id)

  # Cards posted before documents moved into the group still carry a
  # View documents button. Tapping it now posts the files under the card.
  if action == "docs":
    if not _document_rows(target_id):
      await query.answer("No documents on file.", show_alert=True)
      return
    await query.answer()
    await post_documents(context.bot, target_id, query.message.message_id)
    db.log_event(target_id, "documents_posted", actor_user_id=admin_id)
    return

  member = db.get_member(target_id)
  if member is None or member["status"] != db.STATUS_PENDING_ACCESS:
    await query.answer("Already handled.", show_alert=True)
    await query.edit_message_reply_markup(reply_markup=None)
    return
  await query.answer()

  if action == "approve":
    db.set_status(target_id, db.STATUS_ACTIVE, actor_user_id=admin_id)
    await query.edit_message_text(
      f"{query.message.text}\n\n✅ APPROVED by {admin_name}")
    try:
      invite = await invite_for(context.bot, target_id)
    except Exception:
      logger.exception("Could not create invite for %s", target_id)
      invite = None

    await _tell_member(context.bot, target_id, welcome_approved(invite),
                       card_message_id=query.message.message_id)
    return

  if action == "decline":
    await query.edit_message_reply_markup(
      reply_markup=_reason_keyboard(target_id))
    return

  if action == "back":
    await query.edit_message_reply_markup(
      reply_markup=_decision_keyboard(target_id))
    return

  if action == "tog":
    try:
      idx, mask = int(parts[3]), int(parts[4])
    except (IndexError, ValueError):
      return
    mask ^= (1 << idx)                      # XOR toggles that one bit
    await query.edit_message_reply_markup(
      reply_markup=_reason_keyboard(target_id, mask))
    return

  if action == "cfm":
    try:
      reasons = _reasons_from_mask(int(parts[3]))
    except (IndexError, ValueError):
      return
    if not reasons:
      return
    reason = "; ".join(reasons)
    await _finalise_decline(
      context.bot, target_id, admin_id, admin_name, reason,
      card_message_id=query.message.message_id,
      card_text=query.message.text)
    return

  if action == "oth":
    await context.bot.send_message(
      chat_id=ONBOARDING_GROUP_ID,
      text=(_REASON_PROMPT.format(uid=target_id,
                                  mid=query.message.message_id)
            + "\n\nReply to this message with the reason."),
      reply_markup=ForceReply(selective=False),
    )
    return

  logger.warning("Unknown admin action: %r", query.data)


async def handle_decline_reason(update, context: ContextTypes.DEFAULT_TYPE):
  """Catches an admin's reply to the ForceReply prompt."""
  message = update.message
  replied = message.reply_to_message
  if replied is None or replied.from_user is None:
    return
  if replied.from_user.id != context.bot.id:
    return

  match = _REASON_RE.search(replied.text or "")
  if not match:
    return

  target_id = int(match.group(1))
  card_id = int(match.group(2))
  admin = message.from_user
  reason = (message.text or "").strip()
  if not reason:
    return

  ok = await _finalise_decline(
    context.bot, target_id, admin.id,
    admin.first_name or str(admin.id), reason,
    card_message_id=card_id,
  )
  await message.reply_text(
    "Decline recorded and the member has been told." if ok
    else "That member has already been decided.")
