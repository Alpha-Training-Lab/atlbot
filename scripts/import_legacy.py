"""
One-time import of ATL's legacy member spreadsheet into SQLite.

Rows go into holding tables (legacy_members + legacy_responses), NOT into
members. A row only becomes a real member once it is linked to a Telegram
account, which is the next step.

Every value passes through src/kyc_form/validators.py, the same code that cleans
live KYC answers, so imported data looks identical to data from new members.
Values that fail validation are kept with needs_review=1, as KYC does.

Dry run (default) prints a report and writes nothing:
  .venv/bin/python scripts/import_legacy.py data/legacy_members.csv

Write for real:
  .venv/bin/python scripts/import_legacy.py data/legacy_members.csv --commit

The report contains counts and row numbers only, never member data,
so it is safe to share.
"""
import argparse
import csv
import html
import io
import re
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import db  # noqa: E402
from src.db import phone_key, username_key  # noqa: E402  (shared with live matching)
from src.kyc_form import field_by_key, validate  # noqa: E402
# ================================================================================
# Spreadsheet header (lowercased) -> KYC field_key. Every other column is ignored.
SIMPLE_COLUMNS = {
  "email": "email",
  "phone number": "mobile",
  "date of birth": "birthday",
  "country": "country_of_residence",
  "state": "state",
  "select id to upload": "id_type",
  "who introduced you to atl?": "vouch_name",
  "referral/vouch telegram username": "vouch_username",
  "where did you hear about us?": "referral_source",
}
REQUIRED_HEADERS = set(SIMPLE_COLUMNS) | {
  "first name", "last name", "residential address", "zip", "telegram username",
}
FIELD_ORDER = [
  "full_name", "email", "mobile", "birthday", "country_of_residence", "state",
  "address", "referral_source", "vouch_name", "vouch_username", "id_type",
]

# Placeholders people type into forms instead of leaving a cell empty.
JUNK = {"", "-", "--", ".", "n/a", "na", "nil", "nill", "none", "null"}

# Old website wording -> the bot's exact option text. Seen in the real file.
ID_TYPE_ALIASES = {
  "nin": "NIN Slip",
  "driver license": "Driver's Licence",
}

_SCI_RE = re.compile(r"^\d(\.\d+)?e\+\d+$", re.IGNORECASE)   # 2.34803E+12
_EXTRA_DATE_FORMATS = ["%B %d, %Y", "%b %d, %Y", "%B %d %Y", "%b %d %Y"]
_SLASH_DATE_RE = re.compile(r"^(\d{1,2})([/-])(\d{1,2})[/-](\d{2}|\d{4})$")
# ================================================================================

def norm_header(h):
  return " ".join(str(h or "").split()).lower()


def cell_text(value):
  """Turn a CSV cell into clean text."""
  # The old website stored "Voter's" as "Voter\&#39;s": undo both layers.
  v = html.unescape(value or "")
  v = v.replace("\\'", "'").replace('\\"', '"').strip()
  if re.fullmatch(r"\d+\.0", v):
    v = v[:-2]                               # "2348031234567.0" -> "2348031234567"
  return "" if v.lower() in JUNK else v


def prep_birthday(raw, evidence):
  """Reshape spreadsheet date quirks into something the validator reads."""
  v = raw.strip()
  if v.isdigit() and 1 <= int(v) <= 60000:            # Excel serial date
    return (date(1899, 12, 30) + timedelta(days=int(v))).isoformat()
  m = re.match(r"^(\d{4}-\d{2}-\d{2})[ T]", v)        # "1990-07-05 00:00:00"
  if m:
    return m.group(1)
  for fmt in _EXTRA_DATE_FORMATS:                      # "July 5, 1990"
    try:
      return datetime.strptime(v, fmt).date().isoformat()
    except ValueError:
      pass
  m = _SLASH_DATE_RE.match(v)
  if m:
    first, sep, second, year = m.groups()
    if int(first) > 12:
      evidence["day_first"] += 1
    if int(second) > 12:
      evidence["month_first"] += 1
    if len(year) == 2:
      # The year is thrown away anyway. 2000 is a leap year, so 29/02 survives.
      return f"{first}{sep}{second}{sep}2000"
  return v


def read_rows(path):
  raw = Path(path).read_bytes()
  for encoding in ("utf-8-sig", "cp1252"):
    try:
      text = raw.decode(encoding)
      break
    except UnicodeDecodeError:
      continue
  else:
    raise RuntimeError("file is neither UTF-8 nor Windows-1252 text")
  print(f"File encoding: {encoding}")

  reader = csv.DictReader(io.StringIO(text, newline=""))
  headers = [norm_header(h) for h in reader.fieldnames or []]
  missing = REQUIRED_HEADERS - set(headers)
  if missing:
    raise RuntimeError(f"missing columns: {sorted(missing)}")
  reader.fieldnames = headers

  for row_num, row in enumerate(reader, start=2):   # row 1 is the header
    # Too many cells lands under key None; too few leaves values as None.
    # Either way the columns are shifted, so every value may be in the wrong field.
    if None in row or any(v is None for v in row.values()):
      yield row_num, None
      continue
    yield row_num, {h: cell_text(v) for h, v in row.items()}


def build_records(path):
  stats = defaultdict(Counter)
  evidence = Counter()
  records = []
  skipped_empty = 0
  malformed = []

  for row_num, row in read_rows(path):
    if row is None:
      malformed.append(row_num)
      continue
    values = {
      "full_name": " ".join(p for p in (row["first name"], row["last name"]) if p),
      "address": ", ".join(p for p in (row["residential address"], row["zip"]) if p),
    }
    for header, key in SIMPLE_COLUMNS.items():
      values[key] = row[header]

    if _SCI_RE.match(values["mobile"]):
      stats["mobile"]["destroyed"] += 1   # digits already lost; nothing to keep
      values["mobile"] = ""

    uname = username_key(row["telegram username"])
    if not uname and not any(values.values()):
      skipped_empty += 1
      continue

    responses = []
    for key in FIELD_ORDER:
      raw = values[key]
      if not raw:
        stats[key]["blank"] += 1
        continue
      if key == "birthday":
        raw = prep_birthday(raw, evidence)
      if key == "id_type":
        raw = ID_TYPE_ALIASES.get(raw.lower(), raw)
      ok, cleaned, _ = validate(field_by_key(key), raw)
      if ok:
        responses.append((key, cleaned, 0))
        stats[key]["ok"] += 1
      elif key == "birthday":
        # Never store an unreadable birthday as typed: it may contain the
        # year, which ATL deliberately does not keep.
        stats[key]["unreadable"] += 1
      else:
        responses.append((key, raw, 1))
        stats[key]["review"] += 1

    records.append({
      "row": row_num,
      "username_key": uname,
      "phone_key": phone_key(values["mobile"]) if values["mobile"] else None,
      "responses": responses,
    })

  return records, stats, evidence, skipped_empty, malformed


def duplicate_rows(records, key):
  seen = defaultdict(list)
  for r in records:
    if r[key]:
      seen[r[key]].append(r["row"])
  return sorted(row for rows in seen.values() if len(rows) > 1 for row in rows)


def print_report(records, stats, evidence, skipped_empty, malformed):
  def rows_list(rows):
    if not rows:
      return ""
    shown = ", ".join(map(str, rows[:20]))
    return f"[rows {shown}{' ...' if len(rows) > 20 else ''}]"

  total = len(records)
  with_user = sum(1 for r in records if r["username_key"])
  with_phone = sum(1 for r in records if r["phone_key"])
  neither = sum(1 for r in records if not r["username_key"] and not r["phone_key"])
  dup_user = duplicate_rows(records, "username_key")
  dup_phone = duplicate_rows(records, "phone_key")

  print()
  print("ROWS")
  print(f"  to import:               {total}")
  print(f"  empty (skipped):         {skipped_empty}")
  print(f"  malformed (skipped):     {len(malformed)} {rows_list(malformed)}")
  if malformed:
    print("    wrong number of columns, usually an unquoted comma in an address")
  print()
  print("LINKING KEYS")
  print(f"  Telegram username:       {with_user}  ({total - with_user} missing or invalid)")
  print(f"  usable phone number:     {with_phone}")
  print(f"  neither (no auto-link):  {neither}")
  print(f"  duplicate usernames:     {len(dup_user)} rows {rows_list(dup_user)}")
  print(f"  duplicate phones:        {len(dup_phone)} rows {rows_list(dup_phone)}")
  print()
  print(f"FIELDS                     {'ok':>5} {'review':>7} {'blank':>6}")
  for key in FIELD_ORDER:
    s = stats[key]
    print(f"  {key:<24} {s['ok']:>5} {s['review'] + s['unreadable']:>7} {s['blank']:>6}")
  print(f"  (unreadable birthdays are dropped, not stored: {stats['birthday']['unreadable']})")
  print(f"  (phones wrecked by Excel, e.g. 2.34803E+12: {stats['mobile']['destroyed']})")
  print()
  print("BIRTHDAYS TYPED AS TEXT")
  print(f"  day-first evidence:      {evidence['day_first']}")
  print(f"  month-first evidence:    {evidence['month_first']}")
  seen = evidence["day_first"] + evidence["month_first"]
  if seen and evidence["month_first"] / seen > 0.05:
    print("  !! Many dates look month-first. Do NOT commit. Send me this report.")
  elif evidence["month_first"]:
    print("  A few individual month-first dates: they fail to parse and are dropped.")


def commit(records):
  db.init_db()   # creates the legacy tables if they don't exist yet
  with db.get_conn() as conn:   # one transaction: every row or none
    existing = conn.execute("SELECT COUNT(*) FROM legacy_members").fetchone()[0]
    if existing:
      raise RuntimeError(
        f"legacy_members already has {existing} rows. "
        "The import has already run; refusing to import twice.")
    for rec in records:
      cur = conn.execute(
        "INSERT INTO legacy_members (source_row, username_key, phone_key) "
        "VALUES (?, ?, ?)",
        (rec["row"], rec["username_key"], rec["phone_key"]))
      conn.executemany(
        "INSERT INTO legacy_responses (legacy_id, field_key, value_text, needs_review) "
        "VALUES (?, ?, ?, ?)",
        [(cur.lastrowid, key, value, review) for key, value, review in rec["responses"]])


def main():
  parser = argparse.ArgumentParser(description="Import legacy ATL members.")
  parser.add_argument("csv_path", help="path to the .csv file")
  parser.add_argument("--commit", action="store_true", help="actually write to the database")
  args = parser.parse_args()

  records, stats, evidence, skipped_empty, malformed = build_records(args.csv_path)
  print_report(records, stats, evidence, skipped_empty, malformed)
  print()
  if args.commit:
    commit(records)
    print(f"COMMITTED: {len(records)} rows written to legacy_members.")
  else:
    print("DRY RUN: nothing written. Re-run with --commit to import.")



# ==========================================================================
if __name__ == "__main__":
  try:
    main()
  except Exception as exc:
    print(f"FAILED: {exc}")
    sys.exit(1)