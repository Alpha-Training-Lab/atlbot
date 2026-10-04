"""Small Telegram helpers shared by every feature."""
from telegram import ReplyParameters
from telegram.error import BadRequest
# ===========================================================================


def start_link(bot, payload):
  """Deep link that opens a DM with Alpha and sends /start <payload>."""
  return f"https://t.me/{bot.username}?start={payload}"


def mention(user_id, name):
  """An HTML mention; send with parse_mode=ParseMode.HTML."""
  return f'<a href="tg://user?id={user_id}">{name}</a>'


def message_link(chat_id, message_id):
  """Supergroup message link: strip the -100 prefix."""
  return f"https://t.me/c/{str(chat_id).replace('-100', '', 1)}/{message_id}"


def handle_or_name(member):
  """How a member is labelled where admins see them (photo captions, invite
  link names): their @username, or their name if they have none. Their
  Telegram id stays internal; admins never see it."""
  if member is None:
    return "a member"
  if member["username"]:
    return f"@{member['username']}"
  name = f"{member['first_name'] or ''} {member['last_name'] or ''}".strip()
  return name or "a member without a username"


def who(member):
  """'First Last @username' for an admin card."""
  handle = f"@{member['username']}" if member["username"] else "(no username)"
  name = f"{member['first_name'] or ''} {member['last_name'] or ''}".strip()
  return f"{name} {handle}"


async def send_file(bot, chat_id, file_ref, caption=None, reply_to=None):
  """Send back a member's uploaded ID. KYC accepts an ID sent as a photo or
  as a file, and Telegram won't send a file-type upload as a photo, so try
  photo first and fall back to a file."""
  reply = (ReplyParameters(message_id=reply_to, allow_sending_without_reply=True)
           if reply_to else None)
  try:
    return await bot.send_photo(chat_id, photo=file_ref, caption=caption,
                                reply_parameters=reply)
  except BadRequest:
    return await bot.send_document(chat_id, document=file_ref, caption=caption,
                                   reply_parameters=reply)
