"""KYC collector — walks a member through the active field list in DM."""
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, ApplicationHandlerStop
from datetime import datetime

from src import db
from src.kyc_fields import active_fields
from src.validators import validate
# ==============================================================================
MAX_ATTEMPTS = 3


def _keyboard_for(field, field_index):
  """Inline buttons for choice fields; None for everything else."""
  if field["type"] != "choice":
    return None
  rows = [
    [InlineKeyboardButton(opt, callback_data=f"kyc:{field_index}:{i}")]
    for i, opt in enumerate(field.get("options", []))
  ]
  return InlineKeyboardMarkup(rows)


async def send_current_field(bot, user_id):
  """Send whichever question the member is currently on."""
  member = db.get_member(user_id)
  if member is None:
    return
  fields = active_fields()
  idx = member["kyc_field_index"]

  if idx >= len(fields):
    await finish_kyc(bot, user_id)
    return

  field = fields[idx]
  text = f"Question {idx + 1} of {len(fields)}\n\n{field['prompt']}"
  await bot.send_message(
    chat_id=user_id,
    text=text,
    reply_markup=_keyboard_for(field, idx),
  )


async def start_kyc(bot, user_id):
  """Begin registration from the first field."""
  db.set_status(user_id, db.STATUS_KYC_IN_PROGRESS)
  db.advance_kyc(user_id, 0)
  await bot.send_message(
    chat_id=user_id,
    text=("Let's get you registered.\n\n"
          "I'll ask a few questions one at a time. "
          "You can stop and come back later — I'll remember where you left off."),
  )
  await send_current_field(bot, user_id)


async def finish_kyc(bot, user_id):
  db.set_status(user_id, db.STATUS_PENDING_ACCESS)
  await bot.send_message(
    chat_id=user_id,
    text=("That's everything — thank you.\n\n"
          "Your registration has gone to the admin team for final review. "
          "I'll message you here as soon as it's confirmed."),
  )
  # Step 4 will notify the admin group here.


def _save_and_advance(user_id, field, idx, value_text=None,
                      file_ref=None, needs_review=0):
  db.save_kyc_answer(user_id, field["key"], value_text=value_text,
                     file_ref=file_ref, needs_review=needs_review)
  db.advance_kyc(user_id, idx + 1)


def _pretty_day_month(mmdd):
  """'07-05' -> '5 July'"""
  dt = datetime.strptime(f"2000-{mmdd}", "%Y-%m-%d")
  return f"{dt.day} {dt.strftime('%B')}"


async def handle_kyc_message(update, context: ContextTypes.DEFAULT_TYPE):
  """Group 0. Handles a DM only if the sender is mid-KYC."""
  user = update.effective_user
  message = update.message
  if user is None or message is None:
    return

  member = db.get_member(user.id)
  if member is None or member["status"] != db.STATUS_KYC_IN_PROGRESS:
    return  # not our business — let the LLM handler take it

  fields = active_fields()
  idx = member["kyc_field_index"]
  if idx >= len(fields):
    await finish_kyc(context.bot, user.id)
    raise ApplicationHandlerStop

  field = fields[idx]

  # --- document fields want a file, not words ------------------------
  if field["type"] == "document":
    file_ref = None
    if message.photo:
      file_ref = message.photo[-1].file_id      # last = highest resolution
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

  # --- choice fields want a button tap -------------------------------
  if field["type"] == "choice":
    await message.reply_text("Please tap one of the buttons above.")
    raise ApplicationHandlerStop

  # --- everything else is validated text -----------------------------
  raw = message.text
  if raw is None:
    await message.reply_text("Please reply with text for this question.")
    raise ApplicationHandlerStop

  ok, cleaned, error = validate(field, raw)
  if ok:
    _save_and_advance(user.id, field, idx, value_text=cleaned)
    if field["type"] == "day_month":
      await message.reply_text(
        f"Got it — {_pretty_day_month(cleaned)}. "
        "If that's wrong, tell an admin when you're approved.")
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
  """Handles taps on choice-field buttons."""
  query = update.callback_query
  await query.answer()

  user_id = query.from_user.id
  member = db.get_member(user_id)
  if member is None or member["status"] != db.STATUS_KYC_IN_PROGRESS:
    return

  try:
    _, field_idx_s, option_idx_s = query.data.split(":")
    field_idx, option_idx = int(field_idx_s), int(option_idx_s)
  except (ValueError, AttributeError):
    return

  # Stale button from an earlier question
  if field_idx != member["kyc_field_index"]:
    await query.edit_message_reply_markup(reply_markup=None)
    return

  fields = active_fields()
  field = fields[field_idx]
  options = field.get("options", [])
  if option_idx >= len(options):
    return

  chosen = options[option_idx]
  _save_and_advance(user_id, field, field_idx, value_text=chosen)

  await query.edit_message_text(
    f"Question {field_idx + 1} of {len(fields)}\n\n"
    f"{field['prompt']}\n\n✓ {chosen}"
  )
  await send_current_field(context.bot, user_id)


