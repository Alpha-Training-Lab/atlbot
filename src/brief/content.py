"""Fixed text the weekly brief rotates through. Edit freely: add, remove or
reword lines; nothing else needs to change.

The list is shuffled once (the same shuffle every time), then the brief
takes the next item each week. So the order looks random, and nothing
repeats until every item has been used.
"""
import random
from datetime import date
# ===========================================================================

SAFETY_TIPS = [
  "ATL admins will never DM you first to ask for money, passwords or codes. "
  "If someone does, report them to an admin.",
  "Official payment handlers never DM you first. If someone does, report "
  "them to an admin.",
  "Never share your exchange password, 2FA codes or recovery phrase with "
  "anyone, including people who say they're from \"support\".",
  "Anyone promising guaranteed or fixed returns is running a scam. Walk away.",
  "Scammers copy admins' names and photos. Check the @username carefully "
  "before you reply.",
  "Turn on two-step verification for Telegram, your email and every "
  "exchange account you use.",
  "Don't open links or files that strangers send you in DMs, even if they "
  "look official.",
  "\"Account managers\" who offer to trade your money for you are a common "
  "scam. Keep control of your own accounts.",
  "Before paying anyone who says they're from ATL, confirm with an admin in "
  "the main group first.",
  "Never install screen-sharing or remote-access apps because a stranger "
  "asked you to.",
  "Nobody needs you to pay a fee first to release a prize, airdrop or "
  "withdrawal. That is always a scam.",
  "Keep your phone number, address and ID out of public groups.",
]

def pick_for_week(items, week_start, salt):
  """This week's item. salt keeps each list's shuffle different."""
  order = list(items)
  random.Random(salt).shuffle(order)
  week_number = date.fromisoformat(week_start).toordinal() // 7
  return order[week_number % len(order)]
