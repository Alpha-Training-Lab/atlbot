import logging

from telegram import Update
from telegram.ext import (
  Application,
  CommandHandler,
  ContextTypes,
  MessageHandler,
  filters,
  CallbackQueryHandler,
)

from src import db
from config import BOT_TOKEN
from src import leadership
from src.modules.alpha import handle_alpha_message
from src.modules import kyc
# =========================================================================


logging.basicConfig(
  format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
  level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
  payload = context.args[0] if context.args else None

  if payload == leadership.MODULE:
    await leadership.handle_registration(update, context)
    return

  # Future: elif payload == induction.MODULE: ...

  await update.message.reply_text(
    "👋 Hi! I'm Alpha. An ATL task automation Bot. Please use the link shared in your ATL group "
    "so I know what to sign you up for."
  )


async def chat_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
  """Temporary helper: run this inside a group to find its chat id."""
  await update.message.reply_text(f"Chat ID: {update.effective_chat.id}")


async def cmd_kyc(update, context):
  """TEMPORARY test entry point. Remove before go-live."""
  user = update.effective_user
  db.upsert_member(
    user.id,
    username=user.username,
    first_name=user.first_name,
    last_name=user.last_name,
  )
  await kyc.start_kyc(context.bot, user.id)



# ----- Main compilation of handlers and polling loop --------------------------------------
def main() -> None:
  db.init_db()

  app = Application.builder().token(BOT_TOKEN).build()

  # ----- group 0: commands ------------------------------------------------
  app.add_handler(CommandHandler("start", start), group=0)
  app.add_handler(CommandHandler("chatid", chat_id), group=0)
  app.add_handler(CommandHandler("kyc", cmd_kyc), group=0)   # TEMPORARY

  # ----- group 0: KYC collector (runs before Alpha) -----------------------
  app.add_handler(
    MessageHandler(filters.ChatType.PRIVATE & ~filters.COMMAND,
                   kyc.handle_kyc_message),
    group=0,
  )
  app.add_handler(
    CallbackQueryHandler(kyc.handle_kyc_choice, pattern=r"^kyc:"),
    group=0,
  )

  # ----- group 1: Alpha LLM catch-all -------------------------------------
  app.add_handler(
    MessageHandler(
      filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE,
      handle_alpha_message,
    ),
    group=1,
  )

  app.run_polling()



# =========================================================================
if __name__ == "__main__":
  main()