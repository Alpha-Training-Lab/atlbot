import os
from pathlib import Path

from dotenv import load_dotenv
# ================================
load_dotenv()

BASE_DIR = Path(__file__).resolve().parent

# ----- Token/API keys -------------------------
BOT_TOKEN = os.environ["BOT_TOKEN"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]


# ----- Database settings -------------------------
_db_path_env = os.environ.get("ATL_DB_PATH")
DB_PATH = Path(_db_path_env).expanduser() if _db_path_env else BASE_DIR / "atl_bot.db"


# ----- Induction settings -------------------------
MAX_DECLINES = 3
COOLDOWN_SECONDS = 6 * 3600
MIN_INDUCTION_DAYS = 10
MIN_INDUCTION_SECONDS = int(
  os.environ.get("MIN_INDUCTION_SECONDS", MIN_INDUCTION_DAYS * 86400))
INVITE_TTL_SECONDS = 48 * 3600
WELCOME_DELETE_SECONDS = 24 * 3600
REMINDER_DELETE_SECONDS = 3 * 3600
REQUIRED_TAGS = [
  t.strip() for t in os.getenv(
    "REQUIRED_TAGS", "@ShemmyCypher,@Dr_evidence,@Epitome61"
  ).split(",") if t.strip()
]


# ----- Group IDs -------------------------
INDUCTION_GROUP_ID = int(os.getenv("INDUCTION_GROUP_ID", "0"))
ONBOARDING_GROUP_ID = int(os.environ["ONBOARDING_GROUP_ID"] or 0)
LEADERSHIP_GROUP_ID = int(os.environ.get("LEADERSHIP_GROUP_ID") or 0)
MAIN_GROUP_ID = int(os.environ.get("MAIN_GROUP_ID") or 0)

# ----- Group links -------------------------
MAIN_GROUP_LINK = os.environ["MAIN_GROUP"]
INDUCTION_PINNED_URL = os.environ.get("INDUCTION_GROUP_PINNED_MESSAGE")
  