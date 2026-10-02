"""Alpha's entry point: wires every feature's handlers into one bot and runs it.

Telegram updates pass through handler groups in order: -2, -1, 0, then 1.
Within a group, only the FIRST matching handler runs; a handler can also
raise ApplicationHandlerStop to keep an update from reaching later groups.
So the order below is the bot's priority list. Read it top to bottom to see
who gets a message first.
"""
import logging

from telegram import Update
from telegram.ext import (
  Application,
  CallbackQueryHandler,
  ChatJoinRequestHandler,
  ChatMemberHandler,
  CommandHandler,
  MessageHandler,
  filters,
)

from src import commands, db
from src.assistant.chat import handle_alpha_message
from src.common.cleanup import sweep_deletions
from src.config import (BOT_TOKEN, INDUCTION_GROUP_ID, MAIN_GROUP_ID,
                        ONBOARDING_GROUP_ID, OWNER_USER_ID)
from src.members import legacy, main_group, profile, profile_edit, vouch
from src.onboarding import access_review, induction, kyc
# =========================================================================================

logging.basicConfig(
  format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
  level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


async def on_error(update, context):
  logger.exception("Handler error", exc_info=context.error)


def build_app() -> Application:
  app = Application.builder().token(BOT_TOKEN).build()

  if app.job_queue is None:
    logger.error("JobQueue unavailable — install python-telegram-bot[job-queue]")
  else:
    app.job_queue.run_repeating(sweep_deletions, interval=300, first=10)
    app.job_queue.run_repeating(vouch.sweep, interval=1800, first=60)

  # ----- group -2: vouches making contact (members/vouch.py) -------------
  # First of all: a named vouch's "Hi" is how Alpha learns who they are.
  # Stops the update only when it asks them something.
  app.add_handler(
    MessageHandler(filters.ChatType.PRIVATE, vouch.on_private_message),
    group=-2,
  )

  # ----- group -1: legacy member linking (members/legacy.py) -------------
  # Runs before everything else, so by the time Alpha answers, a legacy
  # member is already linked and active. The contact and Skip handlers come
  # first: they stop the update, so a phone number never reaches KYC or the LLM.
  app.add_handler(
    MessageHandler(filters.ChatType.PRIVATE & filters.CONTACT,
                   legacy.handle_contact),
    group=-1,
  )
  app.add_handler(
    MessageHandler(filters.ChatType.PRIVATE & filters.Text([legacy.SKIP_BUTTON]),
                   legacy.handle_skip),
    group=-1,
  )
  app.add_handler(
    # Join/leave notices aren't posts: a new joiner is handled by main_group.py.
    MessageHandler((filters.Chat(MAIN_GROUP_ID) & ~filters.StatusUpdate.ALL)
                   | filters.ChatType.PRIVATE,
                   legacy.passive_link),
    group=-1,
  )

  # ----- group 0: commands (src/commands.py, members/profile.py) ---------
  app.add_handler(CommandHandler("start", commands.start), group=0)
  app.add_handler(CommandHandler("chatid", commands.chat_id), group=0)
  app.add_handler(CommandHandler("kyc", commands.cmd_kyc), group=0)   # TEMPORARY
  app.add_handler(CommandHandler("profile", profile.cmd_profile), group=0)

  # ----- group 0: induction group (onboarding/induction.py) ---------------
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

  # ----- group 0: profile collector (members/profile.py) -----------------
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

  # ----- group 0: profile edits (members/profile_edit.py) ----------------
  # Same rule as above: before the KYC collector, and IN_EDIT keeps it from
  # matching anyone who isn't part-way through an edit.
  app.add_handler(
    MessageHandler(filters.ChatType.PRIVATE & ~filters.COMMAND
                   & profile_edit.IN_EDIT,
                   profile_edit.handle_edit_message),
    group=0,
  )
  app.add_handler(
    CallbackQueryHandler(profile_edit.handle_member_button, pattern=r"^pe:"), group=0)
  app.add_handler(
    CallbackQueryHandler(profile_edit.handle_edit_decision, pattern=r"^pa:"), group=0)

  # ----- group 0: KYC collector (onboarding/kyc.py) ----------------------
  # Matches every private non-command message, but only acts for members
  # mid-registration; anyone else falls through to Alpha in group 1.
  app.add_handler(
    MessageHandler(filters.ChatType.PRIVATE & ~filters.COMMAND,
                   kyc.handle_kyc_message),
    group=0,
  )
  app.add_handler(
    CallbackQueryHandler(kyc.handle_kyc_choice, pattern=r"^kyc:"), group=0)
  app.add_handler(
    CallbackQueryHandler(kyc.handle_restart, pattern=r"^rst:"), group=0)

  # ----- group 0: a vouch's Yes / No (members/vouch.py) -----------------
  app.add_handler(
    CallbackQueryHandler(vouch.handle_answer, pattern=r"^vc:"), group=0)

  # ----- group 0: admin review of registrations (onboarding/access_review.py)
  app.add_handler(
    CallbackQueryHandler(access_review.handle_access_decision, pattern=r"^acc:"),
    group=0,
  )
  app.add_handler(
    MessageHandler(filters.Chat(ONBOARDING_GROUP_ID) & filters.REPLY & filters.TEXT,
                   access_review.handle_decline_reason),
    group=0,
  )

  # ----- group 0: main group joins, leaves, removals (members/main_group.py)
  app.add_handler(ChatJoinRequestHandler(main_group.handle_join_request), group=0)
  app.add_handler(
    ChatMemberHandler(main_group.handle_main_group_member_update,
                      ChatMemberHandler.CHAT_MEMBER, chat_id=MAIN_GROUP_ID),
    group=0,
  )
  if not MAIN_GROUP_ID:
    logger.warning("MAIN_GROUP_ID is not set: legacy linking, invites and "
                   "main-group checks are disabled. Set it in .env.")
  if not OWNER_USER_ID:
    logger.warning("OWNER_USER_ID is not set: unrecorded joins to the main "
                   "group will NOT be reversed. Set it in .env.")

  # ----- group 1: Alpha LLM catch-all (assistant/chat.py) -----------------
  app.add_handler(
    MessageHandler(
      filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE,
      handle_alpha_message,
    ),
    group=1,
  )

  app.add_error_handler(on_error)
  return app


def main() -> None:
  db.init_db()
  build_app().run_polling(allowed_updates=Update.ALL_TYPES)


# =====================================================
if __name__ == "__main__":
  main()
