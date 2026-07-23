import os

BOT_TOKEN = os.environ["BOT_TOKEN"]
LEADERSHIP_GROUP_ID = int(os.environ.get("LEADERSHIP_GROUP_ID") or 0)
DB_PATH = os.environ.get("ATL_DB_PATH", "atl_bot.db")