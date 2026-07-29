"""Alpha conversational module — routes member DMs to the LLM."""

import logging

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import ContextTypes

from src.llm import ask_alpha
# =================================================


logger = logging.getLogger(__name__)

MODULE = "alpha"

async def handle_alpha_message(
  update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
  """Send a member's DM to Alpha and reply with the answer."""
  message = update.message
  if not message or not message.text:
    return

  logger.info("Alpha query from user %s", update.effective_user.id)

  await context.bot.send_chat_action(
    chat_id=message.chat_id, action=ChatAction.TYPING
  )

  reply = await ask_alpha(message.text)
  await message.reply_text(reply)