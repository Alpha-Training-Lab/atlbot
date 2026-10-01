"""Sending a member's uploaded ID back out.

KYC accepts an ID sent as a photo or as a file. Telegram won't send a
file-type upload as a photo, so try photo first and fall back to a file."""
from telegram import ReplyParameters
from telegram.error import BadRequest
# ===========================================================================


async def send_file(bot, chat_id, file_ref, caption=None, reply_to=None):
  reply = (ReplyParameters(message_id=reply_to, allow_sending_without_reply=True)
           if reply_to else None)
  try:
    return await bot.send_photo(chat_id, photo=file_ref, caption=caption,
                                reply_parameters=reply)
  except BadRequest:
    return await bot.send_document(chat_id, document=file_ref, caption=caption,
                                   reply_parameters=reply)
