"""KYC collector — walks a member through the active field list in DM.

Finished registrations go to admins via access_review.py."""
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, ApplicationHandlerStop

from src import db
from src.config import MAX_DECLINES, COOLDOWN_SECONDS
from src.kyc_form import active_fields, display_value, validate
from src.members.main_group import REMOVED_TEXT
from src.onboarding.access_review import send_access_request
# =====================================================================
logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 3
# =====================================================================
def _format_wait(seconds):
  mins = int(seconds // 60)
  h, m = divmod(mins, 60)
  if h and m:
    return f"{h} hour{'s' if h > 1 else ''} and {m} minute{'s' if m > 1 else ''}"
  if h:
    return f"{h} hour{'s' if h > 1 else ''}"
  return f"{max(m, 1)} minute{'s' if m != 1 else ''}"


def seconds_until_retry(user_id):
  """How long a declined member must wait before registering again; 0 means
  now. Below MAX_DECLINES they can retry straight away. From then on there
  is a COOLDOWN_SECONDS wait after each decline."""
  if db.count_events(user_id, "status:declined") < MAX_DECLINES:
    return 0
  elapsed = db.seconds_since_last_event(user_id, "status:declined")
  if elapsed is None:
    return 0
  return max(0, COOLDOWN_SECONDS - elapsed)


def _keyboard_for(field, field_index):
  if field["type"] != "choice":
    return None
  rows = [
    [InlineKeyboardButton(opt, callback_data=f"kyc:{field_index}:{i}")]
    for i, opt in enumerate(field.get("options", []))
  ]
  return InlineKeyboardMarkup(rows)


async def send_current_field(bot, user_id):
  member = db.get_member(user_id)
  if member is None:
    return
  fields = active_fields()
  idx = member["kyc_field_index"]

  if idx >= len(fields):
    await finish_kyc(bot, user_id)
    return

  field = fields[idx]
  await bot.send_message(
    chat_id=user_id,
    text=f"Question {idx + 1} of {len(fields)}\n\n{field['prompt']}",
    reply_markup=_keyboard_for(field, idx),
  )


async def _delete_registration_prompt(bot, user_id):
  """The induction-group 'tap to register' post has done its job."""
  prompt = db.pop_registration_prompt(user_id)
  if prompt is None:
    return
  try:
    await bot.delete_message(prompt["chat_id"], prompt["message_id"])
  except Exception as e:
    logger.warning("Could not delete registration prompt %s: %s",
                   prompt["message_id"], e)


async def start_kyc(bot, user_id):
  db.set_status(user_id, db.STATUS_KYC_IN_PROGRESS)
  db.advance_kyc(user_id, 0)
  await _delete_registration_prompt(bot, user_id)
  await bot.send_message(
    chat_id=user_id,
    text=("Let's get you registered.\n\n"
          "I'll ask a few questions one at a time. You can stop and come "
          "back later — I'll remember where you left off."),
  )
  await send_current_field(bot, user_id)


async def _restart_after_decline(bot, user_id):
  db.log_event(user_id, "kyc_restarted")
  await start_kyc(bot, user_id)


async def finish_kyc(bot, user_id):
  db.set_status(user_id, db.STATUS_PENDING_ACCESS)
  await bot.send_message(
    chat_id=user_id,
    text=("That's everything — thank you.\n\n"
          "Your registration has gone to the admin team for final review. "
          "I'll message you here as soon as it's confirmed."),
  )
  await send_access_request(bot, user_id)


def _save_and_advance(user_id, field, idx, value_text=None,
                      file_ref=None, needs_review=0):
  db.save_kyc_answer(user_id, field["key"], value_text=value_text,
                     file_ref=file_ref, needs_review=needs_review)
  db.advance_kyc(user_id, idx + 1)


async def handle_kyc_entry(bot, user_id):
  """Single entry point for starting or resuming registration."""
  member = db.get_member(user_id)
  if member is None:
    return
  status = member["status"]

  if status == db.STATUS_KYC_IN_PROGRESS:
    await bot.send_message(user_id, "Let's carry on where you left off.")
    await send_current_field(bot, user_id)
    return

  if status == db.STATUS_AWAITING_DM:
    await start_kyc(bot, user_id)
    return

  if status == db.STATUS_DECLINED:
    wait = seconds_until_retry(user_id)
    if wait:
      await bot.send_message(
        user_id,
        f"You've used all {MAX_DECLINES} attempts. You can try again in "
        f"{_format_wait(wait)}, but please speak to an admin in the "
        "induction group first.")
      return
    await _restart_after_decline(bot, user_id)
    return

  if status == db.STATUS_PENDING_ACCESS:
    await bot.send_message(
      user_id,
      "You've already completed registration. The admin team is doing the "
      "final review — I'll message you here as soon as there's a decision.")
    return

  if status == db.STATUS_ACTIVE:
    await bot.send_message(
      user_id, "You're already a full member — nothing more to do here.")
    return

  if status == db.STATUS_REMOVED:
    await bot.send_message(user_id, REMOVED_TEXT)
    return

  # pending_summary / pending_review
  await bot.send_message(
    user_id,
    "You're not ready for registration yet.\n\n"
    "First, finish reading the induction material in the ATL Induction "
    "Center. When you're done, post in that group and tag "
    "@AlphaTrainingLab_bot along with the onboarding admins. Once an "
    "admin approves you, come back here and send /kyc.")


async def handle_kyc_message(update, context: ContextTypes.DEFAULT_TYPE):
  """Group 0. Handles a DM only if the sender is mid-KYC."""
  user = update.effective_user
  message = update.message
  if user is None or message is None:
    return

  member = db.get_member(user.id)
  if member is None or member["status"] != db.STATUS_KYC_IN_PROGRESS:
    return   # not ours — let the LLM handler take it

  fields = active_fields()
  idx = member["kyc_field_index"]
  if idx >= len(fields):
    await finish_kyc(context.bot, user.id)
    raise ApplicationHandlerStop

  field = fields[idx]

  # --- document fields want a file ------------------------------------
  if field["type"] == "document":
    file_ref = None
    if message.photo:
      file_ref = message.photo[-1].file_id     # last = highest resolution
    elif message.document:
      file_ref = message.document.file_id

    if file_ref:
      _save_and_advance(user.id, field, idx, file_ref=file_ref)
      await send_current_field(context.bot, user.id)
      raise ApplicationHandlerStop

    attempts = db.bump_kyc_attempts(user.id)
    if attempts >= MAX_ATTEMPTS:
      _save_and_advance(user.id, field, idx,
                        value_text=(message.text or "")[:500],
                        needs_review=1)
      await context.bot.send_message(
        user.id, "No problem — an admin will follow this one up with you.")
      await send_current_field(context.bot, user.id)
    else:
      await message.reply_text(
        "I need a photo for this one. Please send it as an image or a file.")
    raise ApplicationHandlerStop

  # --- choice fields want a tap ---------------------------------------
  if field["type"] == "choice":
    await message.reply_text("Please tap one of the buttons above.")
    raise ApplicationHandlerStop

  # --- validated text --------------------------------------------------
  raw = message.text
  if raw is None:
    await message.reply_text("Please reply with text for this question.")
    raise ApplicationHandlerStop

  ok, cleaned, error = validate(field, raw)
  if ok:
    _save_and_advance(user.id, field, idx, value_text=cleaned)
    if field["type"] == "day_month":
      await message.reply_text(
        f"Got it — {display_value(field, cleaned)}. "
        "If that's wrong, tell an admin once you're approved.")
    await send_current_field(context.bot, user.id)
    raise ApplicationHandlerStop

  attempts = db.bump_kyc_attempts(user.id)
  if attempts >= MAX_ATTEMPTS:
    _save_and_advance(user.id, field, idx, value_text=raw[:500],
                      needs_review=1)
    await context.bot.send_message(
      user.id, "I'll pass that to an admin to check. Moving on.")
    await send_current_field(context.bot, user.id)
  else:
    await message.reply_text(error)
  raise ApplicationHandlerStop


async def handle_kyc_choice(update, context: ContextTypes.DEFAULT_TYPE):
  """Taps on choice-field buttons."""
  query = update.callback_query
  await query.answer()

  user_id = query.from_user.id
  member = db.get_member(user_id)
  if member is None or member["status"] != db.STATUS_KYC_IN_PROGRESS:
    return

  parts = (query.data or "").split(":")
  if len(parts) < 3:
    logger.warning("Unparseable kyc callback_data: %r", query.data)
    return
  try:
    field_idx, option_idx = int(parts[1]), int(parts[2])
  except ValueError:
    logger.warning("Bad kyc callback_data: %r", query.data)
    return

  fields = active_fields()
  if field_idx != member["kyc_field_index"] or field_idx >= len(fields):
    await query.edit_message_reply_markup(reply_markup=None)   # stale button
    return

  field = fields[field_idx]
  options = field.get("options", [])
  if option_idx >= len(options):
    return

  chosen = options[option_idx]
  _save_and_advance(user_id, field, field_idx, value_text=chosen)

  await query.edit_message_text(
    f"Question {field_idx + 1} of {len(fields)}\n\n"
    f"{field['prompt']}\n\n✓ {chosen}")
  await send_current_field(context.bot, user_id)


async def handle_restart(update, context: ContextTypes.DEFAULT_TYPE):
  """Routes 'rst:' button taps: 'begin' (any status) or 'full' (declined retry).
  Each path answers the button press exactly once: a second answer fails."""
  query = update.callback_query
  user_id = query.from_user.id

  if query.data == "rst:begin":
    await query.answer()
    await handle_kyc_entry(context.bot, user_id)
    return

  member = db.get_member(user_id)
  if member is None or member["status"] != db.STATUS_DECLINED:
    await query.answer()
    return

  wait = seconds_until_retry(user_id)
  if wait:
    await query.answer(f"You can try again in {_format_wait(wait)}.",
                       show_alert=True)
    return

  await query.answer()
  await query.edit_message_reply_markup(reply_markup=None)
  await _restart_after_decline(context.bot, user_id)
