"""Choosing whether the weekly brief may name you.

The answer lives in kyc_responses as the "Weekly brief" field, the same one
new members answer at the end of registration and anyone can change under
/profile. This module is the quick way in:

  /mentions          any member, in a DM with Alpha: Yes / No buttons
  ?start=mentions    deep link to the same thing (commands.py)
  /askmentions       owner only, in a DM: Alpha posts an invite in the main
                     group with a button that opens the deep link

Every choice is logged in member_events (brief_mentions_set), so there is a
record of when someone agreed or changed their mind.
"""
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from src import db
from src.common.telegram_helpers import start_link
from src.config import MAIN_GROUP_ID
from src.kyc_form.fields import (BRIEF_MENTIONS_KEY, BRIEF_MENTIONS_NO,
                                 BRIEF_MENTIONS_YES)
# ===========================================================================

DEEP_LINK = "mentions"

ASK_TEXT = (
  "Every Monday I post a short brief in the induction group, so people "
  "still in induction can see what life inside ATL looks like.\n\n"
  "May I mention you by name in it? For example when the community "
  "celebrates your birthday, or when you've helped others that week.\n\n"
  "{current}You can change this any time with /mentions."
)

GROUP_INVITE = (
  "📣 Every Monday I share a short brief with the people in induction, "
  "giving them a peek at life inside ATL.\n\n"
  "Happy for your name to appear in it, for your birthday or for helping "
  "others? Tap below to choose. Nobody is named unless they say yes."
)

_BUTTONS = InlineKeyboardMarkup([[
  InlineKeyboardButton("✅ Yes, mention me", callback_data="bm:yes"),
  InlineKeyboardButton("No, leave me out", callback_data="bm:no"),
]])


def _current(user_id):
  row = db.get_kyc_answers(user_id).get(BRIEF_MENTIONS_KEY)
  if row is None:
    return "You haven't chosen yet, so you're not named.\n\n"
  if row["value_text"] == BRIEF_MENTIONS_YES:
    return "Right now: you may be named.\n\n"
  return "Right now: you're not named.\n\n"


async def show_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
  """/mentions in a DM, or arriving from the deep link."""
  user = update.effective_user
  db.upsert_member(user.id, username=user.username,
                   first_name=user.first_name, last_name=user.last_name)
  await update.message.reply_text(
    ASK_TEXT.format(current=_current(user.id)), reply_markup=_BUTTONS)


async def handle_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
  query = update.callback_query
  user = query.from_user
  user_id = user.id
  yes = query.data == "bm:yes"
  db.upsert_member(user_id, username=user.username,
                   first_name=user.first_name, last_name=user.last_name)
  db.save_kyc_answer(user_id, BRIEF_MENTIONS_KEY,
                     value_text=BRIEF_MENTIONS_YES if yes else BRIEF_MENTIONS_NO)
  db.log_event(user_id, "brief_mentions_set", note="yes" if yes else "no")
  await query.answer()
  await query.edit_message_text(
    "Thank you! The brief may mention you by name. 🙌" if yes else
    "Done. The brief won't mention you by name.")


async def cmd_ask_mentions(update: Update, context: ContextTypes.DEFAULT_TYPE):
  """Owner only: post the invite in the main group."""
  if not MAIN_GROUP_ID:
    await update.message.reply_text("MAIN_GROUP_ID isn't set in .env.")
    return
  await context.bot.send_message(
    MAIN_GROUP_ID, GROUP_INVITE,
    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(
      "Choose in a private chat", url=start_link(context.bot, DEEP_LINK))]]))
  await update.message.reply_text("Posted in the main group.")
