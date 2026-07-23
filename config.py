import os

BOT_TOKEN = os.environ["BOT_TOKEN"]
LEADERSHIP_GROUP_ID = int(os.environ["LEADERSHIP_GROUP_ID"])
DB_PATH = os.environ.get("ATL_DB_PATH", "atl_bot.db")