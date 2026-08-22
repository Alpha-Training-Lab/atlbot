"""KYC collector — walks a member through the active field list in DM."""
import logging
from datetime import datetime

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, ApplicationHandlerStop

from src import db
from src.kyc_fields import active_fields
from src.validators import validate
from config import MAX_DECLINES, COOLDOWN_SECONDS
# =====================================================================
logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 3
# =====================================================================
def _pretty_day_month(mmdd):
  """'07-05' -> '5 July'"""
  dt = datetime.strptime(f"2000-{mmdd}", "%Y-%m-%d")
  return f"{dt.day} {dt.strftime('%B')}"


def _format_wait(seconds):
  mins = int(seconds // 60)
  h, m = divmod(mins, 60)
  if h and m:
    return f"{h} hour{'s' if h > 1 else ''} and {m} minute{'s' if m > 1 else ''}"
  if h:
    return f"{h} hour{'s' if h > 1 else ''}"
  return f"{max(m, 1)} minute{'s' if m != 1 else ''}"


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


async def start_kyc(bot, user_id):
  db.set_status(user_id, db.STATUS_KYC_IN_PROGRESS)
  db.advance_kyc(user_id, 0)
  await bot.send_message(
    chat_id=user_id,
    text=("Let's get you registered.\n\n"
          "I'll ask a few questions one at a time. You can stop and come "
          "back later — I'll remember where you left off."),
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
  from src.modules import admin      # deferred: avoids a circular import
  await admin.send_access_request(bot, user_id)


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
    declines = db.count_events(user_id, "status:declined")
    if declines >= MAX_DECLINES:
      elapsed = db.seconds_since_last_event(user_id, "status:declined")
      if elapsed is not None and elapsed < COOLDOWN_SECONDS:
        await bot.send_message(
          user_id,
          "You've used all three attempts. You can try again in "
          f"{_format_wait(COOLDOWN_SECONDS - elapsed)}, but please speak "
          "to an admin in the induction group first.")
        return
      await bot.send_message(
        user_id,
        "You've used all three registration attempts. Please contact an "
        "admin in the induction group.")
      return
    db.log_event(user_id, "kyc_restarted")
    await start_kyc(bot, user_id)
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

  # pending_summary / pending_review / removed
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
        f"Got it — {_pretty_day_month(cleaned)}. "
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

  if field_idx != member["kyc_field_index"]:
    await query.edit_message_reply_markup(reply_markup=None)   # stale button
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
    f"{field['prompt']}\n\n✓ {chosen}")
  await send_current_field(context.bot, user_id)


async def handle_restart(update, context: ContextTypes.DEFAULT_TYPE):
  """Routes 'rst:' button taps: 'begin' (any status) or 'full' (declined retry)."""
  query = update.callback_query
  await query.answer()

  user_id = query.from_user.id

  if query.data == "rst:begin":
    await handle_kyc_entry(context.bot, user_id)
    return

  member = db.get_member(user_id)
  if member is None or member["status"] != db.STATUS_DECLINED:
    return

  declines = db.count_events(user_id, "status:declined")
  if declines >= MAX_DECLINES:
    elapsed = db.seconds_since_last_event(user_id, "status:declined")
    if elapsed is not None and elapsed < COOLDOWN_SECONDS:
      await query.answer(
        f"You can try again in {_format_wait(COOLDOWN_SECONDS - elapsed)}.",
        show_alert=True)
      return
    await query.edit_message_reply_markup(reply_markup=None)
    await context.bot.send_message(
      user_id,
      "You've reached the limit of three registration attempts.\n\n"
      "Please contact an admin in the induction group.")
    return

  elapsed = db.seconds_since_last_event(user_id, "status:declined")
  if elapsed is not None and elapsed < COOLDOWN_SECONDS:
    await query.answer(
      f"You can try again in {_format_wait(COOLDOWN_SECONDS - elapsed)}.",
      show_alert=True)
    return

  db.log_event(user_id, "kyc_restarted")
  db.set_status(user_id, db.STATUS_KYC_IN_PROGRESS)
  db.advance_kyc(user_id, 0)
  await query.edit_message_reply_markup(reply_markup=None)
  await send_current_field(context.bot, user_id)