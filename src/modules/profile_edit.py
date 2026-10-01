"""Editing details already on file.

Members pick what to change from a menu grouped by meaning: vouch name and
username together, and ID type plus both ID photos together, so an ID record
can never end up half-changed. Every edit shows current -> new and waits for
the member to confirm.

On confirm, self-edit fields save straight away. Approval fields go to the
onboarding group as ONE card per request, approved or rejected as a whole;
a rejection carries a preset reason the member sees.

Like the fill-missing flow, answers are collected here and stopped before
they can reach Alpha's LLM.
"""
import json
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import Forbidden
from telegram.ext import ApplicationHandlerStop, ContextTypes, filters

from config import ONBOARDING_GROUP_ID
from src import db
from src.files import send_file
from src.kyc_fields import active_fields, field_by_key, label, needs_approval
from src.modules import profile
from src.validators import validate

logger = logging.getLogger(__name__)
# ===========================================================================

MAX_ATTEMPTS = 3

# Fields that only make sense changed together.
GROUPS = {
  "vouch": ("Who vouched for you", ["vouch_name", "vouch_username"]),
  "id":    ("Your ID", ["id_type", "id_document", "id_with_face"]),
}
# A change to these is checked against the ID already on file, so that ID
# is posted under the card for the admin to compare.
CHECK_AGAINST_ID = {"full_name", "birthday"}

REJECT_REASONS = [
  "Photo is blurry or unreadable",
  "Name doesn't match the ID",
  "Birthday doesn't match the ID",
  "The person in the photo doesn't match the ID",
  "Vouch could not be verified",
]

# ----- Messages -------------------------------------------------------------
MENU_TEXT = ("✏️ What would you like to change?\n\n"
             "🔒 means an admin checks it before it changes.")
ALREADY_PENDING = ("You already have a change to this waiting for approval. "
                   "You can change it again once it's been reviewed.")
CANCELLED = "No changes made."
GAVE_UP = "Let's leave that for now. Your details are unchanged."
SAME_AS_BEFORE = "That's the same as what's already on file, so there's nothing to change."
USE_BUTTONS = "Please use the buttons above to save or cancel."
SAVED = "✅ Saved."
SENT_FOR_APPROVAL = ("📨 Sent for approval. Nothing on your profile changes "
                     "until an admin approves it, and I'll message you when they do.")


# ----- Groups -----------------------------------------------------------------

def edit_groups():
  """[(group_key, label, [fields])] in KYC order; grouped fields appear once."""
  member_of = {k: g for g, (_, keys) in GROUPS.items() for k in keys}
  seen, groups = set(), []
  for field in active_fields():
    g = member_of.get(field["key"])
    if g is None:
      groups.append((field["key"], label(field), [field]))
    elif g not in seen:
      seen.add(g)
      g_label, keys = GROUPS[g]
      fields = [field_by_key(k) for k in keys]
      groups.append((g, g_label, [f for f in fields if f and f.get("active")]))
  return groups


def _group(group_key):
  return next((g for g in edit_groups() if g[0] == group_key), None)


def _group_label_for(field_keys):
  for g_key, g_label, fields in edit_groups():
    if {f["key"] for f in fields} == set(field_keys):
      return g_label
  return ", ".join(label(field_by_key(k)) for k in field_keys)


def _current(field, answers):
  row = answers.get(field["key"])
  if row is None or (field["type"] == "document" and not row["file_ref"]):
    return "nothing yet"
  if row["file_ref"]:
    return "a photo"
  return profile.pretty_value(field, row)


class _InEditSession(filters.MessageFilter):
  """Matches only DMs from a member part-way through an edit."""
  def filter(self, message):
    user = message.from_user
    return user is not None and db.get_edit_session(user.id) is not None


IN_EDIT = _InEditSession()


# ----- Member side ------------------------------------------------------------

async def show_menu(bot, user_id):
  if not profile.is_active(user_id):
    await profile.send_not_active(bot, user_id)
    return
  pending = {r["field_key"] for r in db.get_open_changes(user_id)}
  buttons = []
  for g_key, g_label, fields in edit_groups():
    if any(f["key"] in pending for f in fields):
      text = f"⏳ {g_label}"
    elif any(needs_approval(f) for f in fields):
      text = f"🔒 {g_label}"
    else:
      text = g_label
    buttons.append(InlineKeyboardButton(text, callback_data=f"pe:g:{g_key}"))
  rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
  rows.append([InlineKeyboardButton("✖ Close", callback_data="pe:x")])
  await bot.send_message(user_id, MENU_TEXT, reply_markup=InlineKeyboardMarkup(rows))


async def start_edit(bot, user_id, group_key):
  if not profile.is_active(user_id):
    await profile.send_not_active(bot, user_id)
    return
  group = _group(group_key)
  if group is None:
    return
  pending = {r["field_key"] for r in db.get_open_changes(user_id)}
  if any(f["key"] in pending for f in group[2]):
    await bot.send_message(user_id, ALREADY_PENDING)
    return
  await profile.pause_fill(bot, user_id)   # one flow at a time
  db.start_edit_session(user_id, group_key)
  await _ask(bot, user_id)


def _keyboard(field=None, position=None):
  rows = []
  if field is not None and field["type"] == "choice":
    rows = [[InlineKeyboardButton(opt, callback_data=f"pe:c:{position}:{i}")]
            for i, opt in enumerate(field.get("options", []))]
  rows.append([InlineKeyboardButton("✖ Cancel", callback_data="pe:x")])
  return InlineKeyboardMarkup(rows)


async def _ask(bot, user_id):
  session = db.get_edit_session(user_id)
  if session is None:
    return
  _, g_label, fields = _group(session["group_key"])
  position = session["position"]
  if position >= len(fields):
    await _confirm(bot, user_id)
    return
  field = fields[position]
  answers = db.get_kyc_answers(user_id)
  head = f"Editing: {g_label}"
  if len(fields) > 1:
    head += f" (step {position + 1} of {len(fields)})"
  text = f"{head}\n\nOn file now: {_current(field, answers)}\n\n{field['prompt']}"
  if position == 0 and any(needs_approval(f) for f in fields):
    text += "\n\n🔒 An admin checks this before it changes."
  await bot.send_message(user_id, text, reply_markup=_keyboard(field, position))


def _read(field, message):
  """(value_text, file_ref, error). error is None when the answer is usable."""
  if field["type"] == "document":
    if message.photo:
      return None, message.photo[-1].file_id, None   # last = highest resolution
    if message.document:
      return None, message.document.file_id, None
    return None, None, profile.NEED_PHOTO
  if field["type"] == "choice":
    return None, None, profile.TAP_BUTTON
  if message.text is None:
    return None, None, profile.NEED_TEXT
  ok, cleaned, error = validate(field, message.text)
  return (cleaned, None, None) if ok else (None, None, error)


async def handle_edit_message(update, context: ContextTypes.DEFAULT_TYPE):
  """Group 0, only for members mid-edit (see IN_EDIT). Always stops the
  update here, so personal details never reach Alpha's LLM."""
  user = update.effective_user
  message = update.effective_message
  session = db.get_edit_session(user.id)
  if session is None:
    return
  group = _group(session["group_key"])
  if group is None or session["position"] >= len(group[2]):
    await message.reply_text(USE_BUTTONS)   # waiting at the confirm step
    raise ApplicationHandlerStop

  field = group[2][session["position"]]
  value_text, file_ref, error = _read(field, message)
  if error:
    if field["type"] != "choice" and db.bump_edit_attempts(user.id) >= MAX_ATTEMPTS:
      db.end_edit_session(user.id)
      await message.reply_text(GAVE_UP)
    else:
      await message.reply_text(error)
    raise ApplicationHandlerStop

  db.save_edit_draft(user.id, field["key"], value_text, file_ref)
  await _ask(context.bot, user.id)
  raise ApplicationHandlerStop


async def _confirm(bot, user_id):
  session = db.get_edit_session(user_id)
  _, g_label, fields = _group(session["group_key"])
  draft = json.loads(session["draft"])
  answers = db.get_kyc_answers(user_id)

  lines, changed = [], False
  for field in fields:
    new = draft.get(field["key"], {})
    row = answers.get(field["key"])
    if new.get("file_ref"):
      new_text, changed = "the new photo you sent", True
    else:
      new_text = profile.pretty_value(field, {"file_ref": None,
                                              "value_text": new.get("value_text")})
      if row is None or row["needs_review"] or row["value_text"] != new.get("value_text"):
        changed = True
    lines.append(f"{label(field)}\n  From: {_current(field, answers)}\n  To: {new_text}")

  if not changed:
    db.end_edit_session(user_id)
    await bot.send_message(user_id, SAME_AS_BEFORE)
    return

  approval = any(needs_approval(f) for f in fields)
  save = "✅ Send for approval" if approval else "✅ Save"
  await bot.send_message(
    user_id, "Please check this before it's saved:\n\n" + "\n\n".join(lines),
    reply_markup=InlineKeyboardMarkup([[
      InlineKeyboardButton(save, callback_data="pe:save"),
      InlineKeyboardButton("✖ Cancel", callback_data="pe:x"),
    ]]))


async def _save(bot, user_id):
  session = db.get_edit_session(user_id)
  if session is None:
    return
  _, g_label, fields = _group(session["group_key"])
  draft = json.loads(session["draft"])
  if any(f["key"] not in draft for f in fields):
    return   # not finished; a stale button
  db.end_edit_session(user_id)

  if not any(needs_approval(f) for f in fields):
    for f in fields:
      d = draft[f["key"]]
      db.save_kyc_answer(user_id, f["key"], value_text=d["value_text"],
                         file_ref=d["file_ref"])
      db.log_event(user_id, "profile_edited", note=f["key"])
    await bot.send_message(user_id, SAVED)
    return

  answers = db.get_kyc_answers(user_id)
  card = await bot.send_message(
    ONBOARDING_GROUP_ID, _card_text(user_id, fields, draft, answers),
    reply_markup=_card_keyboard())
  for f in fields:
    d = draft[f["key"]]
    db.add_pending_change(user_id, f["key"], d["value_text"], d["file_ref"],
                          card.message_id)

  for f in fields:   # the new photos, under the card
    if draft[f["key"]]["file_ref"]:
      await send_file(bot, ONBOARDING_GROUP_ID, draft[f["key"]]["file_ref"],
                      caption=f"NEW {label(f)}, user {user_id}",
                      reply_to=card.message_id)
  if any(f["key"] in CHECK_AGAINST_ID for f in fields):   # the ID to compare against
    for key in ("id_document", "id_with_face"):
      row = answers.get(key)
      if row and row["file_ref"]:
        await send_file(bot, ONBOARDING_GROUP_ID, row["file_ref"],
                        caption=f"Current {label(field_by_key(key))} on file, user {user_id}",
                        reply_to=card.message_id)

  await bot.send_message(user_id, SENT_FOR_APPROVAL)


async def handle_member_button(update, context: ContextTypes.DEFAULT_TYPE):
  """pe:menu, pe:g:<group>, pe:c:<position>:<option>, pe:save, pe:x"""
  query = update.callback_query
  await query.answer()
  user_id = query.from_user.id
  bot = context.bot
  parts = (query.data or "").split(":")
  action = parts[1] if len(parts) > 1 else ""

  if action == "menu":
    await show_menu(bot, user_id)
    return

  if action == "g" and len(parts) == 3:
    await query.edit_message_reply_markup(reply_markup=None)
    await start_edit(bot, user_id, parts[2])
    return

  if action == "x":
    await query.edit_message_reply_markup(reply_markup=None)
    if db.get_edit_session(user_id) is not None:
      db.end_edit_session(user_id)
      await bot.send_message(user_id, CANCELLED)
    return

  if action == "save":
    await query.edit_message_reply_markup(reply_markup=None)
    await _save(bot, user_id)
    return

  if action == "c" and len(parts) == 4:
    session = db.get_edit_session(user_id)
    try:
      position, option = int(parts[2]), int(parts[3])
    except ValueError:
      return
    if session is None or position != session["position"]:
      await query.edit_message_reply_markup(reply_markup=None)   # stale
      return
    fields = _group(session["group_key"])[2]
    if position >= len(fields):
      return
    field = fields[position]
    options = field.get("options", [])
    if option >= len(options):
      return
    await query.edit_message_text(f"{field['prompt']}\n\n✓ {options[option]}")
    db.save_edit_draft(user_id, field["key"], value_text=options[option])
    await _ask(bot, user_id)


# ----- Admin side -------------------------------------------------------------

def _card_text(user_id, fields, draft, answers):
  member = db.get_member(user_id)
  handle = f"@{member['username']}" if member["username"] else "(no username)"
  name = f"{member['first_name'] or ''} {member['last_name'] or ''}".strip()
  lines = ["PROFILE CHANGE: awaiting approval", "",
           f"Telegram: {name} {handle}", f"User ID: {user_id}", ""]
  for f in fields:
    d = draft[f["key"]]
    new = ("📎 posted below" if d["file_ref"]
           else profile.pretty_value(f, {"file_ref": None, "value_text": d["value_text"]}))
    lines.append(f"{label(f)}\n  Current: {_current(f, answers)}\n  New: {new}")
  if any(f["key"] in CHECK_AGAINST_ID for f in fields):
    on_file = any(answers.get(k) and answers[k]["file_ref"]
                  for k in ("id_document", "id_with_face"))
    lines += ["", "📎 Their current ID is posted below to compare." if on_file
              else "⚠️ No ID on file to compare against."]
  return "\n".join(lines)


def _card_keyboard():
  return InlineKeyboardMarkup([[
    InlineKeyboardButton("✅ Approve", callback_data="pa:ok"),
    InlineKeyboardButton("❌ Reject", callback_data="pa:no"),
  ]])


def _reason_keyboard():
  rows = [[InlineKeyboardButton(r, callback_data=f"pa:r:{i}")]
          for i, r in enumerate(REJECT_REASONS)]
  rows.append([InlineKeyboardButton("← Back", callback_data="pa:back")])
  return InlineKeyboardMarkup(rows)


async def _notify_member(bot, rows, decision, reason):
  user_id = rows[0]["user_id"]
  what = _group_label_for([r["field_key"] for r in rows])
  if decision == "approved":
    text = f"✅ Approved and saved: {what}"
  else:
    text = (f"❌ Not approved: {what}\n\nReason: {reason}\n\n"
            "Your details are unchanged. Send /profile to try again.")
  try:
    await bot.send_message(user_id, text)
  except Forbidden:
    logger.info("Member %s has blocked the bot; edit result not delivered", user_id)


async def handle_edit_decision(update, context: ContextTypes.DEFAULT_TYPE):
  """pa:ok, pa:no, pa:back, pa:r:<reason> on an edit card."""
  query = update.callback_query
  if query.message is None or query.message.chat.id != ONBOARDING_GROUP_ID:
    await query.answer()
    return
  admin = query.from_user
  admin_name = admin.first_name or str(admin.id)
  parts = (query.data or "").split(":")
  action = parts[1] if len(parts) > 1 else ""

  if action == "no":
    await query.answer()
    await query.edit_message_reply_markup(reply_markup=_reason_keyboard())
    return
  if action == "back":
    await query.answer()
    await query.edit_message_reply_markup(reply_markup=_card_keyboard())
    return

  if action == "ok":
    decision, reason = "approved", None
  elif action == "r" and len(parts) == 3 and parts[2].isdigit() \
      and int(parts[2]) < len(REJECT_REASONS):
    decision, reason = "rejected", REJECT_REASONS[int(parts[2])]
  else:
    await query.answer()
    return

  rows = db.decide_card_changes(query.message.message_id, decision,
                                admin.id, admin_name, reason)
  if not rows:
    await query.answer("Already decided.", show_alert=True)
    await query.edit_message_reply_markup(reply_markup=None)
    return

  await query.answer("Approved." if decision == "approved" else "Rejected.")
  status = (f"✅ APPROVED by {admin_name}" if decision == "approved"
            else f"❌ REJECTED by {admin_name}\nReason: {reason}")
  await query.edit_message_text(f"{query.message.text}\n\n{status}")
  await _notify_member(context.bot, rows, decision, reason)
