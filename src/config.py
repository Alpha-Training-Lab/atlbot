"""Settings, read once from the environment (.env at the project root)."""
import os
from pathlib import Path

from dotenv import load_dotenv
# ================================
# The project root, one level above src/. The database and resources/ live here.
BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def _int_env(name, default=0):
  """An integer setting. Unset or blank means the default, never a crash."""
  value = (os.environ.get(name) or "").strip()
  return int(value) if value else default


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
MIN_INDUCTION_SECONDS = _int_env("MIN_INDUCTION_SECONDS", MIN_INDUCTION_DAYS * 86400)
INVITE_TTL_SECONDS = 48 * 3600
WELCOME_DELETE_SECONDS = 24 * 3600
REMINDER_DELETE_SECONDS = 3 * 3600
REGISTRATION_PROMPT_DELETE_SECONDS = 24 * 3600   # fallback if they never tap it
REQUIRED_TAGS = [
  t.strip() for t in os.getenv(
    "REQUIRED_TAGS", "@ShemmyCypher,@Dr_evidence,@Epitome61"
  ).split(",") if t.strip()
]


# ----- Group IDs -------------------------
INDUCTION_GROUP_ID = _int_env("INDUCTION_GROUP_ID")
ONBOARDING_GROUP_ID = int(os.environ["ONBOARDING_GROUP_ID"] or 0)   # must be present
MAIN_GROUP_ID = _int_env("MAIN_GROUP_ID")
# Anyone in this group is automatically an admin (members/roles.py). Alpha
# must be an admin there. Unset = nobody is made admin this way.
LEADERSHIP_GROUP_ID = _int_env("LEADERSHIP_GROUP_ID")

# ----- Main-group gatekeeping -------------------------
# The one person allowed to add members without onboarding (the backdoor),
# and who is alerted when anyone else's join is reversed. Unset = Alpha
# does NOT remove anyone, so a missing value can't lock the owner out.
OWNER_USER_ID = _int_env("OWNER_USER_ID")
# Where removed joiners are pointed to start onboarding (e.g. the induction
# group's invite link). Unset = they're told to contact an ATL admin.
ONBOARDING_ENTRY_URL = os.environ.get("ONBOARDING_ENTRY_URL")

# ----- Profile sessions -------------------------------------
# A "fill in missing details" or "edit my details" session with no answer
# for this long is closed: until then every DM is taken as an answer.
PROFILE_SESSION_IDLE_SECONDS = 60 * 60

# ----- Vouch consent ---------------------------------------
VOUCH_REMIND_SECONDS = 12 * 3600      # nudge a silent vouch this often
VOUCH_EXPIRE_SECONDS = 72 * 3600      # silence this long counts as No
VOUCH_TAG_DELETE_SECONDS = 6 * 3600   # main-group "send me Hi" tag lifetime

# ----- Group links -------------------------
INDUCTION_PINNED_URL = os.environ.get("INDUCTION_GROUP_PINNED_MESSAGE")
