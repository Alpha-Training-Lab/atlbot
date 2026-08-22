"""Alpha conversational module — routes member DMs to the LLM."""
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.ext import ContextTypes

from src import db
from src.llm import ask_alpha
# =============================================
logger = logging.getLogger(__name__)

MODULE = "alpha"
STATUS_CONTEXT = {
  db.STATUS_PENDING_SUMMARY:
    "This member has NOT yet been approved out of the induction group. "
    "Their next step is to finish reading the induction material, then "
    "post in the induction group tagging the onboarding admins. "
    "Registration cannot begin until an admin approves them.",
  db.STATUS_PENDING_REVIEW:
    "This member has posted in the induction group and an admin is "
    "reviewing them. They only need to wait.",
  db.STATUS_AWAITING_DM:
    "This member has been approved out of induction. They should tap the "
    "button in the induction group to start registration with you.",
  db.STATUS_KYC_IN_PROGRESS:
    "This member is part-way through registration. Tell them to answer "
    "the question you last asked.",
  db.STATUS_PENDING_ACCESS:
    "This member has finished registration. The admin team is doing the "
    "final review. They only need to wait.",
  db.STATUS_ACTIVE:
    "This member is fully approved and has group access.",
  db.STATUS_DECLINED:
    "This member's registration was declined. They can fix the issue and "
    "submit again using the button already sent to them.",
}
NO_RECORD = (
  "This person has no record in the ATL database — they have not started "
  "onboarding. Their first step is the induction group."
)
# =============================================================================
def _context_for(member):
  if member is None:
    return NO_RECORD
  return STATUS_CONTEXT.get(member["status"], NO_RECORD)


async def handle_alpha_message(
  update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
  message = update.message
  if not message or not message.text:
    return

  user_id = update.effective_user.id
  logger.info("Alpha query from user %s", user_id)

  await context.bot.send_chat_action(
    chat_id=message.chat_id, action=ChatAction.TYPING
  )

  member = db.get_member(user_id)
  reply = await ask_alpha(message.text, context_note=_context_for(member))

  markup = None
  if member and member["status"] == db.STATUS_AWAITING_DM:
    markup = InlineKeyboardMarkup([[
      InlineKeyboardButton("Start registration", callback_data="rst:begin")
    ]])
  await message.reply_text(reply, reply_markup=markup)