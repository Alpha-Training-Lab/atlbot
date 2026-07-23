import logging

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from src import db
from config import BOT_TOKEN
from src.modules import leadership
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


def main() -> None:
  db.init_db()

  app = Application.builder().token(BOT_TOKEN).build()
  app.add_handler(CommandHandler("start", start))
  app.add_handler(CommandHandler("chatid", chat_id))
  app.run_polling()




# =========================================================================
if __name__ == "__main__":
  main()