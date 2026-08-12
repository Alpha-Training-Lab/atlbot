import os

from dotenv import load_dotenv
# ================================
load_dotenv()

BOT_TOKEN = os.environ["BOT_TOKEN"]
DB_PATH = os.environ.get("ATL_DB_PATH", "atl_bot.db")
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]

MAX_DECLINES = 3
COOLDOWN_SECONDS = 6 * 3600
MIN_INDUCTION_DAYS = 10


# ----- Group IDs -------------------------
INDUCTION_GROUP_ID = int(os.getenv("INDUCTION_GROUP_ID", "0"))
ONBOARDING_GROUP_ID = int(os.environ["ONBOARDING_GROUP_ID"] or 0)
LEADERSHIP_GROUP_ID = int(os.environ.get("LEADERSHIP_GROUP_ID") or 0)

# ----- Group links -------------------------
MAIN_GROUP_LINK = os.environ["MAIN_GROUP"]
INDUCTION_PINNED_URL = os.environ.get("INDUCTION_GROUP_PINNED_MESSAGE")
  