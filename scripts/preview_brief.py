"""Preview the weekly brief exactly as Monday's job would build it, without
posting it anywhere public.

It runs the real pipeline (numbers from SQLite, Gemini's digest, merge and
both checks) against a THROWAWAY COPY of the database, sends the result to
the owner's DM only (OWNER_USER_ID), prints it, then deletes the copy.
The live database and the induction group are never touched.

Run from the project root:
  .venv/bin/python scripts/preview_brief.py --db /path/to/live/atl_bot.db
  .venv/bin/python scripts/preview_brief.py --db /path/to/live/atl_bot.db --sample

--sample  adds made-up main-group chat, birthday wishes and three made-up
          members who said Yes to being mentioned, so every section has
          something to show. Real data is untouched.
--week    the Monday that starts the week to report (default: last full week).
--no-send print only.
"""
import argparse
import asyncio
import sqlite3
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from telegram import Bot, LinkPreviewOptions  # noqa: E402

import src.db.connection as connection  # noqa: E402
from src import db  # noqa: E402
from src.brief.digest import digest_finished_days  # noqa: E402
from src.brief.weekly import compose, gather  # noqa: E402
from src.config import BOT_TOKEN, MAIN_GROUP_ID, OWNER_USER_ID  # noqa: E402
from src.kyc_form.fields import BRIEF_MENTIONS_KEY, BRIEF_MENTIONS_YES  # noqa: E402
# ===========================================================================

# Made-up members for --sample. Ids far above any real Telegram id in use here.
SAMPLE_MEMBERS = [
  (990000001, "Ada (sample)"),
  (990000002, "Tunde (sample)"),
  (990000003, "Chioma (sample)"),
]
ADA, TUNDE, CHIOMA = (m[0] for m in SAMPLE_MEMBERS)

# Birthday wishes "seen" in the felicitation group: (day 0-6, celebrant).
# The last one isn't on record, so it's counted but never named.
SAMPLE_BIRTHDAYS = [
  (1, f"id:{ADA}"), (1, f"id:{ADA}"), (4, f"id:{TUNDE}"), (5, "u:sample_not_on_record"),
]

# (day of the reported week 0-6, hour, sender, replying to, text)
SAMPLE_CHAT = [
  (0, 9, ADA, None, "Good morning family! Tonight's class is on position sizing."),
  (0, 20, ADA, None, "Key point from tonight: decide how much you can lose before you enter, not after."),
  (0, 20, TUNDE, ADA, "This changed how I see my trades. Thank you!"),
  (1, 11, CHIOMA, None, "Can someone explain the difference between a market and a limit order?"),
  (1, 11, TUNDE, CHIOMA, "Market fills now at the best price. Limit waits for your price."),
  (1, 12, CHIOMA, TUNDE, "Clear now, thanks Tunde."),
  (2, 18, TUNDE, None, "BTC will hit 100k by Friday, buy now before it's too late!"),
  (2, 18, ADA, TUNDE, "Reminder: no calls or predictions in this group. We're here to learn."),
  (3, 15, CHIOMA, None, "Happy news: I passed my final accounting exam today!"),
  (3, 15, ADA, CHIOMA, "Congratulations Chioma! Well deserved."),
  (4, 19, ADA, None, "Journal every trade: why you entered, what you felt, what you learned."),
  (4, 19, CHIOMA, ADA, "Starting my trading journal this weekend."),
  (5, 10, TUNDE, None, "Saturday review: I broke my own rules twice this week. Discipline is the hard part."),
  (5, 10, ADA, TUNDE, "Honest reflection. Noticing it is the first step."),
  (6, 16, CHIOMA, None, "Thank you all for a great week of learning."),
]


def last_full_week():
  today = date.today()
  return (today - timedelta(days=today.weekday() + 7)).isoformat()


def copy_database(live_path, copy_path):
  """sqlite3's backup API, reading the live file read-only."""
  src = sqlite3.connect(f"file:{live_path}?mode=ro", uri=True)
  dst = sqlite3.connect(copy_path)
  try:
    src.backup(dst)
  finally:
    src.close()
    dst.close()


def add_sample_data(week_start, main_group_id):
  monday = date.fromisoformat(week_start)
  with db.get_conn() as conn:
    for user_id, name in SAMPLE_MEMBERS:
      conn.execute(
        "INSERT OR REPLACE INTO members (user_id, first_name, status) "
        "VALUES (?, ?, 'active')", (user_id, name))
      conn.execute(
        "INSERT OR REPLACE INTO kyc_responses (user_id, field_key, value_text) "
        "VALUES (?, ?, ?)", (user_id, BRIEF_MENTIONS_KEY, BRIEF_MENTIONS_YES))
  for day, celebrant in SAMPLE_BIRTHDAYS:
    db.save_birthdays(str(monday + timedelta(days=day)), [celebrant])
  with db.get_conn() as conn:
    for i, (day, hour, sender, reply_to, text) in enumerate(SAMPLE_CHAT, 1):
      sent_at = f"{monday + timedelta(days=day)} {hour:02d}:{i:02d}:00"
      conn.execute(
        "INSERT OR IGNORE INTO brief_messages "
        "(chat_id, message_id, user_id, reply_to_user_id, text, sent_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (main_group_id, 9_000_000 + i, sender, reply_to, text, sent_at))


async def run(args):
  db.init_db()
  if args.sample:
    if not MAIN_GROUP_ID:
      sys.exit("MAIN_GROUP_ID is not set in .env; --sample needs it.")
    add_sample_data(args.week, MAIN_GROUP_ID)
    print("Asking Gemini to digest the sample days...")
    await digest_finished_days()

  async with Bot(BOT_TOKEN) as bot:
    stats = await gather(args.week)
    text, used_gemini = compose(stats)
    label = ("🧪 PREVIEW, not posted anywhere\n"
             f"Week of {args.week} · {'sample data added' if args.sample else 'real data only'}"
             f" · Gemini's part: {'included' if used_gemini else 'NOT included'}\n"
             "────────────\n\n")
    print("\n" + label + text + f"\n\n({len(text)} characters)")
    if stats["hidden_numbers"]:
      print("Hidden from WEEK IN NUMBERS (below BRIEF_HIDE_BELOW): "
            + "; ".join(stats["hidden_numbers"]))
    if args.no_send:
      return
    if not OWNER_USER_ID:
      sys.exit("OWNER_USER_ID is not set in .env, so there's nobody to send it to.")
    await bot.send_message(OWNER_USER_ID, label + text,
                           link_preview_options=LinkPreviewOptions(is_disabled=True))
    print("Sent to your DM.")


def main():
  parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
  parser.add_argument("--db", required=True, help="path to the live atl_bot.db")
  parser.add_argument("--week", default=last_full_week(), help="Monday, YYYY-MM-DD")
  parser.add_argument("--sample", action="store_true")
  parser.add_argument("--no-send", action="store_true")
  args = parser.parse_args()

  live = Path(args.db).expanduser().resolve()
  if not live.is_file():
    sys.exit(f"No database at {live}")
  if date.fromisoformat(args.week).weekday() != 0:
    sys.exit("--week must be a Monday.")

  # The copy holds member data: it lives in a private temp folder and is
  # deleted when this block ends, even if something fails.
  with tempfile.TemporaryDirectory(prefix="brief-preview-") as tmp:
    copy = Path(tmp) / "preview.db"
    copy_database(live, copy)
    # Every query goes through get_conn(), which opens connection.DB_PATH.
    # Point it at the copy, then check, so nothing can reach the live file.
    connection.DB_PATH = copy
    with db.get_conn() as conn:
      opened = Path(conn.execute("PRAGMA database_list").fetchone()["file"])
    if opened.resolve() != copy.resolve():
      sys.exit(f"Refusing to run: queries would go to {opened}, not the copy.")
    asyncio.run(run(args))


if __name__ == "__main__":
  main()
