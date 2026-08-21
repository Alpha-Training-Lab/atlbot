"""Admin review card for the post-KYC access gate."""
import logging
import re

from datetime import datetime, timedelta, timezone
from telegram import (
  ForceReply,
  InlineKeyboardButton,
  InlineKeyboardMarkup,
)
from telegram.ext import ContextTypes


from config import ONBOARDING_GROUP_ID, MAX_DECLINES, MAIN_GROUP_ID, INVITE_TTL_SECONDS
from src import db
from src.kyc_fields import active_fields
from src.messages import welcome_approved
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
# ===========================================================================
# --- card building ----------------------------------------------------
def _label(field):
  return field.get("label") or field["key"].replace("_", " ").capitalize()


def build_kyc_card(user_id):
  member = db.get_member(user_id)
  answers = db.get_kyc_answers(user_id)

  handle = f"@{member['username']}" if member["username"] else "(no username)"
  name = f"{member['first_name'] or ''} {member['last_name'] or ''}".strip()

  lines = [
    "NEW REGISTRATION — awaiting access approval",
    "",
    f"Telegram: {name} {handle}",
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
      value = "[document uploaded]"
    else:
      value = row["value_text"] or "—"
    flag = "  ⚠️ NEEDS REVIEW" if row and row["needs_review"] else ""
    lines.append(f"{_label(field)}: {value}{flag}")

  return "\n".join(lines)


def _decision_keyboard(user_id):
  return InlineKeyboardMarkup([
    [InlineKeyboardButton("📄 View documents",
                          callback_data=f"acc:docs:{user_id}")],
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


async def _personal_invite(bot, user_id):
  link = await bot.create_chat_invite_link(
    chat_id=MAIN_GROUP_ID,
    name=f"member-{user_id}"[:32],
    member_limit=1,
    expire_date=datetime.now(timezone.utc)
                + timedelta(seconds=INVITE_TTL_SECONDS),
  )
  return link.invite_link


async def send_access_request(bot, user_id):
  await bot.send_message(
    chat_id=ONBOARDING_GROUP_ID,
    text=build_kyc_card(user_id),
    reply_markup=_decision_keyboard(user_id),
  )


# --- decline ----------------------------------------------------------
async def _finalise_decline(bot, target_id, admin_id, admin_name, reason,
                            card_message_id=None, card_text=None):
  """Set status, tell the member, update the card. Returns False if
  the member was already decided."""
  member = db.get_member(target_id)
  if member is None or member["status"] != db.STATUS_PENDING_ACCESS:
    return False

  db.set_status(target_id, db.STATUS_DECLINED,
                actor_user_id=admin_id, note=reason)

  declines = db.count_events(target_id, "status:declined")
  at_cap = declines >= MAX_DECLINES

  if at_cap:
    text = ("Your ATL registration was not approved.\n\n"
            f"Reasons: {reason}\n\n"
            "You have now used all three attempts. Please contact an admin "
            "in the induction group — and note there is a 6-hour wait "
            "before any further submission.")
    markup = None
  else:
    # left = MAX_DECLINES - declines
    text = ("Your ATL registration was not approved.\n\n"
            f"Reason: {reason}\n\n"
            "You can fix this and submit again straight away. ")
            # f"You have {left} attempt{'s' if left > 1 else ''} left.")
    markup = InlineKeyboardMarkup([[
      InlineKeyboardButton("Start registration again",
                           callback_data="rst:full")
    ]])

  await bot.send_message(chat_id=target_id, text=text, reply_markup=markup)

  if card_message_id:
    body = card_text or f"User {target_id}"
    await bot.edit_message_text(
      chat_id=ONBOARDING_GROUP_ID,
      message_id=card_message_id,
      text=f"{body}\n\n❌ DECLINED by {admin_name}\nReason: {reason}",
    )
  return True


# --- callbacks --------------------------------------------------------
async def handle_access_decision(update, context: ContextTypes.DEFAULT_TYPE):
  query = update.callback_query
  await query.answer()

  if query.message is None or query.message.chat.id != ONBOARDING_GROUP_ID:
    return

  parts = (query.data or "").split(":")
  if len(parts) < 3:
    logger.warning("Unparseable callback_data: %r", query.data)
    return

  action = parts[1]
  try:
    target_id = int(parts[2])
  except ValueError:
    logger.warning("Bad user id in callback_data: %r", query.data)
    return

  admin_id = query.from_user.id
  admin_name = query.from_user.first_name or str(admin_id)

  # Documents can be viewed at any point, decided or not.
  if action == "docs":
    answers = db.get_kyc_answers(target_id)
    sent = 0
    for field in active_fields():
      row = answers.get(field["key"])
      if row and row["file_ref"]:
        await context.bot.send_photo(
          chat_id=admin_id,
          photo=row["file_ref"],
          caption=f"{field['key']} — user {target_id}",
        )
        sent += 1
    db.log_event(target_id, "documents_viewed", actor_user_id=admin_id)
    if sent == 0:
      await query.answer("No documents on file.", show_alert=True)
    return

  member = db.get_member(target_id)
  if member is None or member["status"] != db.STATUS_PENDING_ACCESS:
    await query.answer("Already handled.", show_alert=True)
    await query.edit_message_reply_markup(reply_markup=None)
    return

  if action == "approve":
    db.set_status(target_id, db.STATUS_ACTIVE, actor_user_id=admin_id)
    await query.edit_message_text(
      f"{query.message.text}\n\n✅ APPROVED by {admin_name}")
    try:
      invite = await _personal_invite(context.bot, target_id)
    except Exception:
      logger.exception("Could not create invite for %s", target_id)
      invite = None

    await context.bot.send_message(chat_id=target_id, text=welcome_approved(invite))
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
    if len(parts) < 5:
      return
    idx, mask = int(parts[3]), int(parts[4])
    mask ^= (1 << idx)                      # XOR toggles that one bit
    await query.edit_message_reply_markup(
      reply_markup=_reason_keyboard(target_id, mask))
    return

  if action == "cfm":
    if len(parts) < 4:
      return
    reasons = _reasons_from_mask(int(parts[3]))
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

