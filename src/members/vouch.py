"""Vouch consent: when a member names a vouch, the vouch confirms or refuses.

A vouch is named by Telegram username. Alpha can't look up someone's id from
their username, and can't message anyone who hasn't started it. So it asks a
vouch directly only when it already knows them; otherwise it tags them in the
main group ("send me Hi"), and their Hi reveals who they are.

A vouch must be a full ATL member, i.e. in the main group. A silent vouch is
chased every VOUCH_REMIND_SECONDS; silence for VOUCH_EXPIRE_SECONDS counts
as No.

  registration  Yes unlocks the admin's Approve button. No, silence, or a
                vouch outside the main group declines it automatically.
  profile       Yes saves the vouch details; anything else discards them.
                Either way the onboarding card shows it; it never holds up
                the member's other details.
"""
import logging
import time

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import Forbidden, TelegramError
from telegram.ext import ApplicationHandlerStop, ContextTypes

from src import db
from src.common.telegram_helpers import start_link
from src.config import (MAIN_GROUP_ID, VOUCH_EXPIRE_SECONDS,
                        VOUCH_REMIND_SECONDS, VOUCH_TAG_DELETE_SECONDS)
from src.kyc_form import FIELD_GROUPS
from src.members.main_group import IN, UNKNOWN, main_group_state
from src.onboarding import access_review
# ===========================================================================
logger = logging.getLogger(__name__)

VOUCH_FIELDS = FIELD_GROUPS["vouch"][1]
REASK_AFTER_SECONDS = 3600   # don't repeat the question on every message
_last_tag = {}               # vouch_key -> when they were last tagged

# ----- Messages -------------------------------------------------------------
NOT_A_USERNAME = ("That doesn't look like a Telegram username. "
                  "Please send it like @username.")
NOT_YOURSELF = ("You can't vouch for yourself. Please give the username of the "
                "ATL member who introduced you.")
NOT_ON_RECORD = ("I can't find @{k} in ATL's records. Please double-check their "
                 "username (it's the one on their Telegram profile, starting "
                 "with @) and send it again.")
REFER_REGISTRATION = ("I still can't find that username in ATL's records, so I've "
                      "passed your vouch to an admin, who will sort it out with "
                      "you. Moving on.")
REFER_PROFILE = ("I still can't find that username in ATL's records. Please "
                 "contact an ATL admin to sort out your vouch.")
NOTHING_TO_CONFIRM = "There's nothing waiting for you to confirm right now. Thank you!"
VOUCH_NOT_MEMBER = ("Thank you for getting in touch. Only full ATL members (in "
                    "the main ATL group) can vouch for someone, so I can't take "
                    "your confirmation.")
_HOURS = VOUCH_EXPIRE_SECONDS // 3600
_REASONS = {
  "no":         "Your vouch (@{k}) did not confirm that they vouch for you.",
  "expired":    f"Your vouch (@{{k}}) didn't respond within {_HOURS} hours.",
  "not_member": ("Your vouch (@{k}) isn't a full ATL member. A vouch must be "
                 "in the main ATL group."),
}


# ----- Checking the username a member gives -----------------------------------

def check_vouch_username(user, raw):
  """(cleaned, error). cleaned is '@name' when it's a usable username, not
  the member's own, and on ATL's records; otherwise error says why. Run
  after the field's normal validation."""
  key = db.username_key(raw)
  if key is None:
    return None, NOT_A_USERNAME
  own = getattr(user, "username", None)
  if own and db.username_key(own) == key:
    return None, NOT_YOURSELF
  # Telegram can't tell a bot whether a person's username exists, so check
  # ATL's own records. This is what stops a typo being tagged in the group.
  if not db.username_on_record(key):
    return None, NOT_ON_RECORD.format(k=key)
  return f"@{key}", None


# ----- Asking the vouch -------------------------------------------------------

def _who_asked(user_id):
  member = db.get_member(user_id)
  if member is None:
    return "An ATL member"
  if member["username"]:
    return f"@{member['username']}"
  name = f"{member['first_name'] or ''} {member['last_name'] or ''}".strip()
  return name or "An ATL member"


def _question(req, reminder=False):
  text = (f"Hi! {_who_asked(req['user_id'])} has selected you as their vouch.\n\n"
          "Do you agree that you vouch for this person to be an ATL member?")
  return f"⏰ Reminder\n\n{text}" if reminder else text


def _answer_keyboard(request_id):
  return InlineKeyboardMarkup([[
    InlineKeyboardButton("✅ Yes, I vouch", callback_data=f"vc:y:{request_id}"),
    InlineKeyboardButton("❌ No", callback_data=f"vc:n:{request_id}"),
  ]])


async def _ask_directly(bot, req, vouch_user_id, reminder=False):
  """DM the question. True if Telegram delivered it."""
  try:
    await bot.send_message(vouch_user_id, _question(req, reminder),
                           reply_markup=_answer_keyboard(req["id"]))
  except Forbidden:
    return False   # they've never started Alpha
  except TelegramError:
    logger.warning("Could not message a vouch", exc_info=True)
    return False
  db.set_vouch_contact(req["id"], vouch_user_id)
  db.mark_vouch_nudged(req["id"])
  return True


async def _tag_in_group(bot, req):
  """'@vouch, please send me Hi' in the main group, deleted after a while.
  One tag per vouch at a time, however many members named them."""
  if not MAIN_GROUP_ID:
    return
  key = req["vouch_key"]
  last = _last_tag.get(key)
  if last is None or time.monotonic() - last >= VOUCH_TAG_DELETE_SECONDS:
    try:
      sent = await bot.send_message(
        MAIN_GROUP_ID, f"@{key}, please send me Hi in your DM. Thank you.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(
          "Message Alpha", url=start_link(bot, "vouch"))]]))
    except TelegramError:
      logger.warning("Could not tag a vouch in the main group", exc_info=True)
      return
    _last_tag[key] = time.monotonic()
    db.schedule_deletion(sent.chat_id, sent.message_id, VOUCH_TAG_DELETE_SECONDS)
  db.mark_vouch_nudged(req["id"])


async def _reach(bot, req, reminder=False):
  """Ask directly if Alpha knows the vouch and they're in the main group,
  otherwise tag them. Never refuses a vouch here: a stored username can be
  out of date, so only the person who actually answers is judged."""
  vouch_id = req["vouch_user_id"]
  if vouch_id is None:
    known = db.find_member_by_username(req["vouch_key"])
    vouch_id = known["user_id"] if known else None
  if vouch_id is not None and await main_group_state(bot, vouch_id) == IN:
    if await _ask_directly(bot, req, vouch_id, reminder):
      return
  await _tag_in_group(bot, req)


async def request_for_registration(bot, user_id, card_message_id):
  """After KYC: ask the named vouch and show it on the admin card. No
  request if the username was unusable; the card flags that for an admin."""
  row = db.get_kyc_answers(user_id).get("vouch_username")
  key = (db.username_key(row["value_text"])
         if row and not row["needs_review"] else None)
  if key is None:
    return None
  request_id = db.create_vouch_request(user_id, key, "registration",
                                       card_message_id)
  await access_review.refresh_card(bot, user_id, card_message_id)
  await _reach(bot, db.get_vouch_request(request_id))
  return request_id


async def request_for_profile(bot, user_id, vouch_username):
  """A member filled in or changed their vouch: ask that vouch. Returns the
  vouch's username key, or None if it was unusable."""
  key = db.username_key(vouch_username)
  if key is None:
    return None
  request_id = db.create_vouch_request(user_id, key, "profile")
  await _reach(bot, db.get_vouch_request(request_id))
  return key


def discard_orphans(user_id):
  """Held vouch details with no request behind them (the member stopped
  between the name and the username) would wait forever: drop them."""
  req = db.latest_vouch_request(user_id, "profile")
  if req is not None and req["status"] == "pending":
    return
  ids = [c["id"] for c in db.get_open_changes(user_id)
         if c["field_key"] in VOUCH_FIELDS]
  db.decide_changes(ids, "rejected", None, "vouch details incomplete")


# ----- The vouch makes contact --------------------------------------------------

async def on_private_message(update, context: ContextTypes.DEFAULT_TYPE):
  """Group -2: every DM, before anything else. If the sender is someone's
  named vouch, ask them now: their 'Hi' is how Alpha learns who they are."""
  user = update.effective_user
  if user is None or user.is_bot or not user.username:
    return
  key = db.username_key(user.username)
  if key is None:
    return
  requests = db.open_requests_for_vouch(key)
  if not requests:
    return

  state = await main_group_state(context.bot, user.id)
  if state == UNKNOWN:
    return   # can't check membership right now; they'll be reminded later
  handled = False
  for req in requests:
    if state != IN:
      if await settle(context.bot, req, "not_member"):
        handled = True
      continue
    if (req["vouch_user_id"] == user.id
        and req["since_nudge_seconds"] < REASK_AFTER_SECONDS):
      continue   # they already have this question
    if await _ask_directly(context.bot, req, user.id):
      handled = True
  if handled and state != IN:
    await update.effective_message.reply_text(VOUCH_NOT_MEMBER)
  if handled:
    raise ApplicationHandlerStop   # don't also answer their "Hi" with the LLM


async def handle_answer(update, context: ContextTypes.DEFAULT_TYPE):
  """vc:y:<request> / vc:n:<request>, tapped by the vouch."""
  query = update.callback_query
  parts = (query.data or "").split(":")
  try:
    answer, request_id = parts[1], int(parts[2])
  except (IndexError, ValueError):
    await query.answer()
    return

  req = db.get_vouch_request(request_id)
  if req is None or req["status"] != "pending":
    await query.answer("This has already been settled. Thank you.", show_alert=True)
    await query.edit_message_reply_markup(reply_markup=None)
    return
  user = query.from_user
  if (user.id != req["vouch_user_id"]
      or db.username_key(user.username or "") != req["vouch_key"]):
    await query.answer("This question isn't for you.", show_alert=True)
    return
  state = await main_group_state(context.bot, user.id, use_cache=False)
  if state == UNKNOWN:
    await query.answer("I couldn't check that just now. Please tap again "
                       "in a minute.", show_alert=True)
    return
  await query.answer()

  who = _who_asked(req["user_id"])
  if state != IN:
    outcome, text = "not_member", VOUCH_NOT_MEMBER
  elif answer == "y":
    outcome, text = "yes", f"✅ Thank you. I've recorded that you vouch for {who}."
  else:
    outcome, text = "no", f"Recorded: you don't vouch for {who}. Thank you for telling me."
  await query.edit_message_text(text)
  await settle(context.bot, req, outcome)


# ----- Outcomes -----------------------------------------------------------------

async def _tell(bot, user_id, text):
  try:
    await bot.send_message(user_id, text)
  except TelegramError:
    logger.info("Could not tell member %s about their vouch", user_id)


async def settle(bot, req, outcome):
  """Close a request and act on it. False if it was already closed."""
  if not db.decide_vouch_request(req["id"], outcome):
    return False
  if req["purpose"] == "registration":
    await _settle_registration(bot, req, outcome)
  else:
    await _settle_profile(bot, req, outcome)
  return True


async def _settle_registration(bot, req, outcome):
  user_id, card = req["user_id"], req["card_message_id"]
  key = req["vouch_key"]
  if outcome == "yes":
    if card:
      await access_review.refresh_card(bot, user_id, card)
    await _tell(bot, user_id, f"✅ @{key} has confirmed they vouch for you. "
                              "An admin will review your registration next.")
    return
  await access_review.decline_registration(
    bot, user_id, _REASONS[outcome].format(k=key), card)


_CARD_OUTCOME = {
  "no":         "vouch said No",
  "expired":    f"no answer from the vouch within {_HOURS} hours",
  "not_member": "vouch isn't in the main group",
}


async def _settle_profile(bot, req, outcome):
  from src.members import profile   # deferred: profile imports this module
  user_id, key = req["user_id"], req["vouch_key"]
  changes = [c for c in db.get_open_changes(user_id) if c["field_key"] in VOUCH_FIELDS]
  ids = [c["id"] for c in changes]
  cards = {c["card_message_id"] for c in changes if c["card_message_id"]}
  if outcome == "yes":
    db.decide_changes(ids, "approved", req["vouch_user_id"], f"@{key} consented")
    text = (f"✅ @{key} has confirmed they vouch for you. "
            "Your vouch details are saved.")
  else:
    db.decide_changes(ids, "rejected", req["vouch_user_id"],
                      _CARD_OUTCOME[outcome], reason=outcome)
    text = (f"❌ Not saved. {_REASONS[outcome].format(k=key)}\n\n"
            "You can name a different vouch from /profile.")
  for card in cards:
    await profile.refresh_card(bot, card)
  await _tell(bot, user_id, text)


# ----- Reminders and expiry ---------------------------------------------------

async def sweep(context: ContextTypes.DEFAULT_TYPE):
  """Repeating job: chase silent vouches, and close requests that ran out."""
  for req in db.open_vouch_requests_with_age():
    try:
      if req["age_seconds"] >= VOUCH_EXPIRE_SECONDS:
        await settle(context.bot, req, "expired")
      elif req["since_nudge_seconds"] >= VOUCH_REMIND_SECONDS:
        await _reach(context.bot, req, reminder=True)
    except Exception:
      logger.exception("Vouch sweep failed for request %s", req["id"])
