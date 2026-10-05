"""Who finished updating their records through Alpha first. Read-only.

  .venv/bin/python scripts/first_completers.py                  # top 100
  .venv/bin/python scripts/first_completers.py --names-only     # ready to paste
  .venv/bin/python scripts/first_completers.py --approved-only  # admin-approved only
  .venv/bin/python scripts/first_completers.py --include-vouch  # vouch required too
  .venv/bin/python scripts/first_completers.py --top 50 --since 2026-10-03

Who counts: active members who updated their profile through Alpha on or
after --since (new registrations don't count; that was a different task),
and whose every required detail is in.

"In" means saved, approved, or (by default) submitted and awaiting admin
approval, so the order reflects when members acted, not how fast the queue
moved. --approved-only counts approved details only, at approval time.
The vouch is left out by default: few members can name one yet.

Ranked by when their LAST required detail went in. Prints @username, or
the name if they have none, never Telegram ids.
"""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.db.connection import get_conn  # noqa: E402
from src.kyc_form import FIELD_GROUPS, active_fields  # noqa: E402
# ===========================================================================

PROFILE_EVENTS = ("profile_updated", "profile_edited", "profile_change_requested")


def completion(answers, changes, keys, documents, approved_only):
  """(completed_at, still_waiting) or None if a required detail is missing."""
  times, waiting = [], False
  for key in keys:
    row = answers.get(key)
    on_file = row is not None and not row["needs_review"] and bool(
      row["file_ref"] if key in documents else row["value_text"])
    approved = [c for c in changes.get(key, []) if c["decision"] == "approved"]
    open_ = [c for c in changes.get(key, []) if c["decision"] is None]
    if on_file:
      if approved and not approved_only:
        times.append(approved[-1]["requested_at"])   # when they sent it
      else:
        times.append(row["created_at"])              # when it was saved / approved
    elif open_ and not approved_only:
      times.append(open_[-1]["requested_at"])
      waiting = True
    else:
      return None
  return max(times), waiting


def main():
  ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
  ap.add_argument("--top", type=int, default=100)
  ap.add_argument("--since", default="2026-10-03", help="UTC date the announcement went out")
  ap.add_argument("--approved-only", action="store_true")
  ap.add_argument("--include-vouch", action="store_true")
  ap.add_argument("--names-only", action="store_true", help="just the ranked names")
  args = ap.parse_args()

  vouch = set(FIELD_GROUPS["vouch"][1])
  fields = [f for f in active_fields() if f.get("required", True)
            and (args.include_vouch or f["key"] not in vouch)]
  keys = [f["key"] for f in fields]
  documents = {f["key"] for f in fields if f["type"] == "document"}
  marks = ",".join("?" * len(PROFILE_EVENTS))

  ranked = []
  with get_conn() as conn:
    people = conn.execute(
      f"""
      SELECT DISTINCT m.user_id, m.username, m.first_name, m.last_name
      FROM member_events e JOIN members m ON m.user_id = e.user_id
      WHERE e.event IN ({marks}) AND e.created_at >= ? AND m.status = 'active'
      """,
      (*PROFILE_EVENTS, args.since),
    ).fetchall()
    for p in people:
      answers = {r["field_key"]: r for r in conn.execute(
        "SELECT field_key, value_text, file_ref, needs_review, created_at "
        "FROM kyc_responses WHERE user_id = ?", (p["user_id"],))}
      changes = {}
      for c in conn.execute(
          "SELECT field_key, requested_at, decision FROM pending_changes "
          "WHERE user_id = ? ORDER BY requested_at, id", (p["user_id"],)):
        changes.setdefault(c["field_key"], []).append(c)
      done = completion(answers, changes, keys, documents, args.approved_only)
      if done is not None and done[0] >= args.since:
        ranked.append((done[0], p["user_id"], p, done[1]))

  ranked.sort(key=lambda r: (r[0], r[1]))
  top = ranked[:args.top]

  def label(p):
    if p["username"]:
      return f"@{p['username']}"
    return f"{p['first_name'] or ''} {p['last_name'] or ''}".strip() or "(no name)"

  if args.names_only:
    for i, (_, _, p, _) in enumerate(top, 1):
      print(f"{i}. {label(p)}")
    return

  rule = "approved details only" if args.approved_only else "submitted, approved or awaiting approval"
  vouch_rule = "vouch included" if args.include_vouch else "vouch not required"
  waiting_total = sum(1 for r in ranked if r[3])
  print(f"FIRST {args.top} TO COMPLETE THEIR RECORDS  (since {args.since}, times UTC)")
  print(f"Counting: every required detail in ({rule}; {vouch_rule})")
  print(f"Completed so far: {len(ranked)} members"
        + (f", {waiting_total} with a detail still awaiting admin approval" if waiting_total else ""))
  print()
  for i, (at, _, p, waiting) in enumerate(top, 1):
    print(f"{i:>4}. {label(p):<32} {at[:16]}{'  *' if waiting else ''}")
  if any(r[3] for r in top):
    print("\n* still has a detail awaiting admin approval")


if __name__ == "__main__":
  main()
