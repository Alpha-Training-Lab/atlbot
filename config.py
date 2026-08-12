import os

from dotenv import load_dotenv
# ================================
load_dotenv()

BOT_TOKEN = os.environ["BOT_TOKEN"]
LEADERSHIP_GROUP_ID = int(os.environ.get("LEADERSHIP_GROUP_ID") or 0)
DB_PATH = os.environ.get("ATL_DB_PATH", "atl_bot.db")
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
ONBOARDING_GROUP_ID = int(os.environ["ONBOARDING_GROUP_ID"] or 0)

MAX_DECLINES = 3
COOLDOWN_SECONDS = 6 * 3600


# ----- Group links -------------------------
MAIN_GROUP_LINK = os.environ["MAIN_GROUP"]