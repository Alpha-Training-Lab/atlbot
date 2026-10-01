"""Member profile: view your details, fill in what's missing.

Any active member can use it; legacy members simply have more blanks.
Fields marked "edit": "self" in kyc_fields.py are saved straight away.
Everything else (identity and vouch details) is held in pending_changes
until an admin in the onboarding group approves it.

Answers are collected here, never by the LLM: the collector stops each
message before it can reach Alpha.
"""
import logging
from datetime import datetime

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest, Forbidden
from telegram.ext import ApplicationHandlerStop, ContextTypes, filters

from config import ONBOARDING_GROUP_ID
from src import db
from src.files import send_file
from src.kyc_fields import active_fields, field_by_key, label, needs_approval
from src.validators import validate

logger = logging.getLogger(__name__)
# ===========================================================================

MAX_ATTEMPTS = 3
VIEW_DELETE_SECONDS = 10 * 60   # the profile view holds personal data

# ----- Messages -------------------------------------------------------------
NOT_ACTIVE = ("Your profile is for ATL members. If you're already in the main "
              "ATL group, tap below so I can recognise you.")
NOTHING_MISSING = "✅ Nothing is missing. Thank you."
FILL_INTRO = ("Let's fill in your missing details, one at a time. "
              "Tap ⏸ Stop for now whenever you like and carry on later.")
APPROVAL_NOTE = "\n\n(An admin checks this one before it's saved.)"
NEED_PHOTO = "I need a photo for this one. Please send it as an image or a file."
NEED_TEXT = "Please reply with text for this one."
TAP_BUTTON = "Please tap one of the buttons above."
SKIPPED = "Let's skip that one for now. You can try again later from /profile."
FINISHED = ("That's everything for now ✅\n\nAnything that needs an admin "
            "check is with them, and I'll message you once it's reviewed. "
            "Send /profile any time to see your details.")
STOPPED = "Paused. Send /profile whenever you want to carry on."
# =================================================================================
# ----- Helpers ----------------------------------------------------------------
def pretty_value(field, row):
  if row["file_ref"]:
    return "on file"
  value = row["value_text"] or "—"
  if field["type"] == "day_month" and len(value) == 5:
    try:
      dt = datetime.strptime(f"2000-{value}", "%Y-%m-%d")
      return f"{dt.day} {dt.strftime('%B')}"
    except ValueError:
      pass
  return value


def field_states(user_id):
  """[(field, row, state)] where state is ok / missing / review / pending."""
  answers = db.get_kyc_answers(user_id)
  pending = {r["field_key"] for r in db.get_open_changes(user_id)}
  states = []
  for field in active_fields():
    row = answers.get(field["key"])
    if field["key"] in pending:
      state = "pending"
    elif row is None:
      state = "missing"
    elif row["needs_review"]:
      state = "review"
    elif field["type"] == "document" and not row["file_ref"]:
      state = "missing"
    else:
      state = "ok"
    states.append((field, row, state))
  return states


def is_active(user_id):
  member = db.get_member(user_id)
  return member is not None and member["status"] == db.STATUS_ACTIVE


class _InProfileSession(filters.MessageFilter):
  """Matches only DMs from someone mid-way through filling their profile,
  so this collector never swallows messages meant for KYC or Alpha."""
  def filter(self, message):
    user = message.from_user
    return user is not None and db.get_profile_session(user.id) is not None


IN_SESSION = _InProfileSession()


# ----- View -------------------------------------------------------------------

async def send_not_active(bot, user_id):
  """Not active yet: say so, and offer the way in for existing members."""
  await bot.send_message(user_id, NOT_ACTIVE, reply_markup=InlineKeyboardMarkup([[
    InlineKeyboardButton("I'm already an ATL member",
                         url=f"https://t.me/{bot.username}?start=link")]]))

async def show_profile(bot, user_id):
  if not is_active(user_id):
    await send_not_active(bot, user_id)
    return

  states = field_states(user_id)
  lines = ["👤 YOUR ATL PROFILE", ""]
  for field, row, state in states:
    if state == "ok":
      lines.append(f"✅ {label(field)}: {pretty_value(field, row)}")
    elif state == "pending":
      lines.append(f"⏳ {label(field)}: waiting for admin approval")
    elif state == "review":
      lines.append(f"⚠️ {label(field)}: needs updating")
    else:
      lines.append(f"❌ {label(field)}: missing")

  to_fill = sum(1 for _, _, s in states if s in ("missing", "review"))
  lines += ["", f"This message deletes itself in {VIEW_DELETE_SECONDS // 60} minutes."]
  rows = []
  if to_fill:
    rows.append([InlineKeyboardButton(
      f"➕ Fill in missing details ({to_fill})", callback_data="pf:fill")])
  rows.append([InlineKeyboardButton("✏️ Edit my details", callback_data="pe:menu")])
  markup = InlineKeyboardMarkup(rows)

  sent = await bot.send_message(user_id, "\n".join(lines), reply_markup=markup)
  db.schedule_deletion(user_id, sent.message_id, VIEW_DELETE_SECONDS)


async def cmd_profile(update, context: ContextTypes.DEFAULT_TYPE):
  await show_profile(context.bot, update.effective_user.id)


# ----- Fill missing -----------------------------------------------------------
def _question_keyboard(field, position):
  rows = []
  if field["type"] == "choice":
    rows = [[InlineKeyboardButton(opt, callback_data=f"pf:c:{position}:{i}")]
            for i, opt in enumerate(field.get("options", []))]
  rows.append([InlineKeyboardButton("⏸ Stop for now", callback_data="pf:stop")])
  return InlineKeyboardMarkup(rows)


async def _ask_current(bot, user_id):
  session = db.get_profile_session(user_id)
  if session is None:
    return
  keys = session["field_keys"].split(",")
  position = session["position"]
  if position >= len(keys):
    await _finish(bot, user_id, FINISHED)
    return
  field = field_by_key(keys[position])
  text = f"Missing detail {position + 1} of {len(keys)}\n\n{field['prompt']}"
  if needs_approval(field):
    text += APPROVAL_NOTE
  await bot.send_message(user_id, text,
                         reply_markup=_question_keyboard(field, position))


async def start_fill(bot, user_id):
  if not is_active(user_id):
    await send_not_active(bot, user_id)
    return
  db.end_edit_session(user_id)   # one flow at a time; an unconfirmed edit is dropped
  keys = [f["key"] for f, _, s in field_states(user_id)
          if s in ("missing", "review")]
  if not keys:
    await bot.send_message(user_id, NOTHING_MISSING)
    return
  db.start_profile_session(user_id, keys)
  await bot.send_message(user_id, FILL_INTRO)
  await _ask_current(bot, user_id)


async def _finish(bot, user_id, text):
  session = db.get_profile_session(user_id)
  card_id = session["card_message_id"] if session else None
  db.end_profile_session(user_id)
  await bot.send_message(user_id, text)
  if card_id:
    await _maybe_notify_member(bot, user_id, card_id)


async def pause_fill(bot, user_id):
  """Stop a fill session if one is running, e.g. when the member switches
  to editing. Anything already sent for approval stays with the admins."""
  if db.get_profile_session(user_id) is not None:
    await _finish(bot, user_id, STOPPED)


async def _submit(bot, user_id, field, value_text=None, file_ref=None):
  """Save a self-edit field now, or hold an approval field for admins."""
  if not needs_approval(field):
    db.save_kyc_answer(user_id, field["key"], value_text=value_text,
                       file_ref=file_ref)
    db.log_event(user_id, "profile_updated", note=field["key"])
    return

  session = db.get_profile_session(user_id)
  card_id = session["card_message_id"] if session else None
  change_id = db.add_pending_change(user_id, field["key"], value_text,
                                    file_ref, card_id)
  if card_id:
    await _refresh_card(bot, card_id)
  else:
    text, markup = _build_card(user_id, [db.get_change(change_id)])
    sent = await bot.send_message(ONBOARDING_GROUP_ID, text, reply_markup=markup)
    card_id = sent.message_id
    db.attach_change_to_card(change_id, card_id)
    db.set_profile_card(user_id, card_id)
  if file_ref:
    await send_file(bot, ONBOARDING_GROUP_ID, file_ref,
                    caption=f"{label(field)}, user {user_id}", reply_to=card_id)


async def _accept(bot, user_id, field, value_text=None, file_ref=None):
  await _submit(bot, user_id, field, value_text, file_ref)
  db.advance_profile_session(user_id)
  await _ask_current(bot, user_id)


async def _fail(bot, message, user_id, error_text):
  if db.bump_profile_attempts(user_id) >= MAX_ATTEMPTS:
    await message.reply_text(SKIPPED)
    db.advance_profile_session(user_id)
    await _ask_current(bot, user_id)
  else:
    await message.reply_text(error_text)


async def handle_profile_message(update, context: ContextTypes.DEFAULT_TYPE):
  """Group 0, only for members mid-session (see IN_SESSION). Always stops
  the update here, so personal details never reach Alpha's LLM."""
  user = update.effective_user
  message = update.effective_message
  bot = context.bot
  session = db.get_profile_session(user.id)
  if session is None:
    return

  keys = session["field_keys"].split(",")
  if session["position"] >= len(keys):
    await _finish(bot, user.id, FINISHED)
    raise ApplicationHandlerStop
  field = field_by_key(keys[session["position"]])

  if field["type"] == "document":
    file_ref = None
    if message.photo:
      file_ref = message.photo[-1].file_id   # last = highest resolution
    elif message.document:
      file_ref = message.document.file_id
    if file_ref:
      await _accept(bot, user.id, field, file_ref=file_ref)
    else:
      await _fail(bot, message, user.id, NEED_PHOTO)
    raise ApplicationHandlerStop

  if field["type"] == "choice":
    await message.reply_text(TAP_BUTTON)
    raise ApplicationHandlerStop

  if message.text is None:
    await _fail(bot, message, user.id, NEED_TEXT)
    raise ApplicationHandlerStop

  ok, cleaned, error = validate(field, message.text)
  if not ok:
    await _fail(bot, message, user.id, error)
    raise ApplicationHandlerStop
  if field["type"] == "day_month":
    await message.reply_text(f"Got it: {pretty_value(field, {'file_ref': None, 'value_text': cleaned})}.")
  await _accept(bot, user.id, field, value_text=cleaned)
  raise ApplicationHandlerStop


async def handle_member_button(update, context: ContextTypes.DEFAULT_TYPE):
  """pf:menu, pf:fill, pf:stop, pf:c:<position>:<option>"""
  query = update.callback_query
  await query.answer()
  user_id = query.from_user.id
  parts = (query.data or "").split(":")
  action = parts[1] if len(parts) > 1 else ""

  if action == "menu":
    await show_profile(context.bot, user_id)
    return
  if action == "fill":
    await start_fill(context.bot, user_id)
    return

  session = db.get_profile_session(user_id)
  if session is None:
    await query.edit_message_reply_markup(reply_markup=None)   # stale button
    return

  if action == "stop":
    await query.edit_message_reply_markup(reply_markup=None)
    await _finish(context.bot, user_id, STOPPED)
    return

  if action == "c" and len(parts) == 4:
    try:
      position, option = int(parts[2]), int(parts[3])
    except ValueError:
      return
    keys = session["field_keys"].split(",")
    if position != session["position"] or position >= len(keys):
      await query.edit_message_reply_markup(reply_markup=None)   # stale
      return
    field = field_by_key(keys[position])
    options = field.get("options", [])
    if option >= len(options):
      return
    await query.edit_message_text(f"{field['prompt']}\n\n✓ {options[option]}")
    await _accept(context.bot, user_id, field, value_text=options[option])


# ----- Admin approval card -------------------------------------------------
def _build_card(user_id, changes):
  member = db.get_member(user_id)
  handle = f"@{member['username']}" if member["username"] else "(no username)"
  name = f"{member['first_name'] or ''} {member['last_name'] or ''}".strip()
  lines = ["PROFILE DETAILS: awaiting approval", "",
           f"Telegram: {name} {handle}", f"User ID: {user_id}", ""]

  buttons = []
  for c in changes:
    field = field_by_key(c["field_key"])
    name_ = label(field) if field else c["field_key"]
    value = "📎 posted below" if c["file_ref"] else pretty_value(field, c) if field else c["value_text"]
    if c["decision"] == "approved":
      status = f"✅ approved by {c['decided_by_name']}"
    elif c["decision"] == "rejected":
      status = f"❌ rejected by {c['decided_by_name']}"
    else:
      status = "⏳"
      buttons.append([
        InlineKeyboardButton(f"✅ {name_}", callback_data=f"pc:a:{c['id']}"),
        InlineKeyboardButton(f"❌ {name_}", callback_data=f"pc:r:{c['id']}"),
      ])
    lines.append(f"{name_}: {value}  {status}")

  return "\n".join(lines), InlineKeyboardMarkup(buttons) if buttons else None


async def _refresh_card(bot, card_message_id):
  changes = db.get_card_changes(card_message_id)
  if not changes:
    return
  text, markup = _build_card(changes[0]["user_id"], changes)
  try:
    await bot.edit_message_text(text, chat_id=ONBOARDING_GROUP_ID,
                                message_id=card_message_id, reply_markup=markup)
  except BadRequest as e:
    if "not modified" not in str(e).lower():
      logger.warning("Could not refresh approval card %s: %s", card_message_id, e)


async def _maybe_notify_member(bot, user_id, card_message_id):
  """Tell the member once: every item on the card decided, session over."""
  changes = db.get_card_changes(card_message_id)
  if not changes or any(c["decision"] is None for c in changes):
    return
  session = db.get_profile_session(user_id)
  if session and session["card_message_id"] == card_message_id:
    return   # still adding items; tell them when they finish

  def names(decision):
    return ", ".join(label(field_by_key(c["field_key"])) for c in changes
                     if c["decision"] == decision)
  approved, rejected = names("approved"), names("rejected")
  lines = ["Your details have been reviewed."]
  if approved:
    lines.append(f"\n✅ Approved and saved: {approved}")
  if rejected:
    lines.append(f"\n❌ Not approved: {rejected}\nPlease check photos are clear "
                 "and details match your ID, then send /profile to try again.")
  try:
    await bot.send_message(user_id, "\n".join(lines))
  except Forbidden:
    logger.info("Member %s has blocked the bot; review result not delivered", user_id)


async def handle_change_decision(update, context: ContextTypes.DEFAULT_TYPE):
  """pc:a:<change_id>, pc:r:<change_id> on the onboarding card."""
  query = update.callback_query
  if query.message is None or query.message.chat.id != ONBOARDING_GROUP_ID:
    await query.answer()
    return

  admin = query.from_user
  admin_name = admin.first_name or str(admin.id)
  card_id = query.message.message_id
  parts = (query.data or "").split(":")
  action = parts[1] if len(parts) > 1 else ""

  if action not in ("a", "r") or len(parts) != 3:
    await query.answer()
    return
  try:
    change_id = int(parts[2])
  except ValueError:
    await query.answer()
    return

  decision = "approved" if action == "a" else "rejected"
  if not db.decide_change(change_id, decision, admin.id, admin_name):
    await query.answer("Already decided.", show_alert=True)
  else:
    await query.answer("Approved." if decision == "approved" else "Rejected.")

  await _refresh_card(context.bot, card_id)
  change = db.get_change(change_id)
  if change:
    await _maybe_notify_member(context.bot, change["user_id"], card_id)