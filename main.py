import logging

from telegram import Update
from telegram.ext import (
  Application,
  CallbackQueryHandler,
  ChatJoinRequestHandler,
  CommandHandler,
  ContextTypes,
  MessageHandler,
  filters,
)

from config import BOT_TOKEN, INDUCTION_GROUP_ID, MAIN_GROUP_ID, ONBOARDING_GROUP_ID
from src import db
from src.modules import admin, kyc, leadership, induction, legacy, profile
from src.modules.alpha import handle_alpha_message
# =========================================================================================

logging.basicConfig(
  format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
  level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
  payload = context.args[0] if context.args else None

  if payload == "kyc":
    user = update.effective_user
    db.upsert_member(user.id, username=user.username,
                     first_name=user.first_name, last_name=user.last_name)
    await kyc.handle_kyc_entry(context.bot, user.id)
    return

  if payload == "link":
    await legacy.handle_link_start(update, context)
    return

  member = db.get_member(update.effective_user.id)
  if member is not None and member["status"] == db.STATUS_ACTIVE:
    await profile.show_profile(context.bot, update.effective_user.id)
    return

  await update.message.reply_text(
    "👋 Hi! I'm Alpha, an ATL task automation bot. Please use the link "
    "shared in your ATL group so I know what to sign you up for."
  )


async def chat_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
  """Temporary helper: run inside a group to find its chat id."""
  await update.message.reply_text(f"Chat ID: {update.effective_chat.id}")


async def cmd_kyc(update, context):
  user = update.effective_user
  db.upsert_member(
    user.id,
    username=user.username,
    first_name=user.first_name,
    last_name=user.last_name,
  )
  await kyc.handle_kyc_entry(context.bot, user.id)


async def on_error(update, context):
  logger.exception("Handler error", exc_info=context.error)



def main() -> None:
  db.init_db()

  app = Application.builder().token(BOT_TOKEN).build()

  if app.job_queue is None:
    logger.error("JobQueue unavailable — install python-telegram-bot[job-queue]")
  else:
    app.job_queue.run_repeating(induction.sweep_deletions,
                                interval=300, first=10)

  # ----- group -1: legacy member linking ------------------------------
  # Runs before everything else, so by the time Alpha answers, a legacy
  # member is already linked and active. Order matters within a group:
  # only the first matching handler runs, so the contact handler goes first.
  app.add_handler(
    MessageHandler(filters.ChatType.PRIVATE & filters.CONTACT,
                   legacy.handle_contact),
    group=-1,
  )
  app.add_handler(
    MessageHandler(filters.Chat(MAIN_GROUP_ID) | filters.ChatType.PRIVATE,
                   legacy.passive_link),
    group=-1,
  )

  # ----- group 0: commands ---------------------------------------------
  app.add_handler(CommandHandler("start", start), group=0)
  app.add_handler(CommandHandler("chatid", chat_id), group=0)
  app.add_handler(CommandHandler("kyc", cmd_kyc), group=0)   # TEMPORARY
  app.add_handler(CommandHandler("profile", profile.cmd_profile), group=0)

  # ----- group 0: induction group --------------------------------------
  app.add_handler(
    MessageHandler(filters.Chat(INDUCTION_GROUP_ID)
                   & filters.StatusUpdate.NEW_CHAT_MEMBERS,
                   induction.handle_new_member),
    group=0,
  )
  app.add_handler(
    MessageHandler(filters.Chat(INDUCTION_GROUP_ID) & filters.TEXT
                   & ~filters.COMMAND,
                   induction.handle_induction_post), group=0)
  app.add_handler(
    CallbackQueryHandler(induction.handle_induction_decision,
                         pattern=r"^ind:"), group=0)

  # ----- group 0: profile collector ------------------------------------
  # Must come before the KYC collector: within a group only the first
  # matching handler runs, and IN_SESSION makes this one match only for
  # members part-way through filling their profile.
  app.add_handler(
    MessageHandler(filters.ChatType.PRIVATE & ~filters.COMMAND
                   & profile.IN_SESSION,
                   profile.handle_profile_message),
    group=0,
  )
  app.add_handler(
    CallbackQueryHandler(profile.handle_member_button, pattern=r"^pf:"), group=0)
  app.add_handler(
    CallbackQueryHandler(profile.handle_change_decision, pattern=r"^pc:"), group=0)

  # ----- group 0: KYC collector ----------------------------------------
  app.add_handler(
    MessageHandler(filters.ChatType.PRIVATE & ~filters.COMMAND,
                   kyc.handle_kyc_message),
    group=0,
  )
  app.add_handler(
    CallbackQueryHandler(kyc.handle_kyc_choice, pattern=r"^kyc:"), group=0)
  app.add_handler(
    CallbackQueryHandler(kyc.handle_restart, pattern=r"^rst:"), group=0)

  # ----- group 0: admin review -----------------------------------------
  app.add_handler(
    CallbackQueryHandler(admin.handle_access_decision, pattern=r"^acc:"),
    group=0,
  )
  app.add_handler(
    MessageHandler(filters.Chat(ONBOARDING_GROUP_ID) & filters.REPLY & filters.TEXT,
                   admin.handle_decline_reason),
    group=0,
  )

  app.add_handler(ChatJoinRequestHandler(induction.handle_join_request), group=0)

  # ----- group 1: Alpha LLM catch-all ----------------------------------
  app.add_handler(
    MessageHandler(
      filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE,
      handle_alpha_message,
    ),
    group=1,
  )


  app.add_error_handler(on_error)
  app.run_polling(allowed_updates=Update.ALL_TYPES)



# =====================================================
if __name__ == "__main__":
  main()