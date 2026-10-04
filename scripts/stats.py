"""A read-only snapshot of how Alpha is doing. Changes nothing.

Run from the project root:
  .venv/bin/python scripts/stats.py

Prints counts only, never anyone's details, so it's safe to paste anywhere.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.db.connection import get_conn  # noqa: E402
# ===========================================================================

DAY = "datetime('now', '-1 day')"


def one(conn, sql, *args):
  return conn.execute(sql, args).fetchone()[0]


def main():
  with get_conn() as conn:
    now = one(conn, "SELECT datetime('now')")
    by_status = dict(conn.execute(
      "SELECT status, COUNT(*) FROM members GROUP BY status").fetchall())

    def events_24h(where, *args):
      return one(conn, f"SELECT COUNT(*) FROM member_events "
                       f"WHERE created_at > {DAY} AND ({where})", *args)

    active = by_status.get("active", 0)
    new_active = events_24h("event = 'status:active'")
    via_spreadsheet = events_24h("event = 'legacy_linked'")
    no_record = events_24h("event = 'status:active' AND note LIKE 'main-group member, no old record%'")
    legacy_total = one(conn, "SELECT COUNT(*) FROM legacy_members")
    legacy_linked = one(conn, "SELECT COUNT(*) FROM legacy_members WHERE linked_user_id IS NOT NULL")

    saved = events_24h("event IN ('profile_updated', 'profile_edited')")
    requested = events_24h("event = 'profile_change_requested' "
                           "AND note NOT IN ('vouch_name', 'vouch_username')")
    waiting_items = one(conn, "SELECT COUNT(*) FROM pending_changes WHERE decision IS NULL "
                              "AND field_key NOT IN ('vouch_name', 'vouch_username')")
    waiting_cards = one(conn, "SELECT COUNT(DISTINCT card_message_id) FROM pending_changes "
                              "WHERE decision IS NULL AND card_message_id IS NOT NULL "
                              "AND field_key NOT IN ('vouch_name', 'vouch_username')")
    oldest_wait = one(conn, "SELECT ROUND((julianday('now') - julianday(MIN(requested_at))) * 24, 1) "
                            "FROM pending_changes WHERE decision IS NULL "
                            "AND field_key NOT IN ('vouch_name', 'vouch_username')")

    admins = one(conn, "SELECT COUNT(*) FROM member_roles WHERE role = 'admin'")
    leader_admins = one(conn, "SELECT COUNT(*) FROM member_roles WHERE role = 'admin' "
                              "AND source = 'leadership'")
    specials = one(conn, "SELECT COUNT(*) FROM special_members WHERE user_id IS NOT NULL")
    specials_waiting = one(conn, "SELECT COUNT(*) FROM special_members WHERE user_id IS NULL")
    vouch_open = one(conn, "SELECT COUNT(*) FROM vouch_requests WHERE status = 'pending'")
    vouch_24h = dict(conn.execute(
      f"SELECT status, COUNT(*) FROM vouch_requests WHERE decided_at > {DAY} "
      "GROUP BY status").fetchall())

  onboarding = sum(by_status.get(s, 0) for s in ("pending_summary", "awaiting_dm", "kyc_in_progress"))

  print(f"ATL SNAPSHOT  {now} UTC  (last 24h in brackets)\n")
  print("MEMBERS")
  print(f"  active                      {active:>5}   (+{new_active})")
  print(f"  registrations to review     {by_status.get('pending_access', 0):>5}")
  print(f"  part-way through onboarding {onboarding:>5}")
  print(f"  declined / removed          {by_status.get('declined', 0):>5} / {by_status.get('removed', 0)}")
  print(f"  🛡 admins                    {admins:>5}   ({leader_admins} from the leadership group)")
  print(f"  ⭐ Legacy Members            {specials:>5}   ({specials_waiting} waiting to be seen)")
  print()
  print("OLD-WEBSITE BACKLOG")
  pct = f"{100 * legacy_linked / legacy_total:.0f}%" if legacy_total else "-"
  print(f"  linked to Telegram          {legacy_linked:>5} of {legacy_total} ({pct})   (+{via_spreadsheet})")
  print(f"  recognised, no old record   ({no_record} in the last 24h)")
  print()
  print("PROFILES")
  print(f"  details saved by members    ({saved})")
  print(f"  sent for admin approval     ({requested})")
  wait = f", oldest {oldest_wait}h" if oldest_wait is not None else ""
  cards = "card" if waiting_cards == 1 else "cards"
  print(f"  WAITING ON ADMINS           {waiting_items:>5} items on {waiting_cards} {cards}{wait}")
  print()
  print("VOUCHES")
  print(f"  waiting for consent         {vouch_open:>5}")
  print(f"  answered                    (yes {vouch_24h.get('yes', 0)}, no {vouch_24h.get('no', 0)}, "
        f"expired {vouch_24h.get('expired', 0)}, not a member {vouch_24h.get('not_member', 0)})")


if __name__ == "__main__":
  main()
