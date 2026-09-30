"""
Nightly SQLite backup for ATLbot.

1. Takes a safe hot copy of the live database (safe while the bot is running).
2. Checks the copy is not corrupted.
3. Uploads it to S3.
4. Keeps the newest local copies, deletes older ones.

Run from the project root:
  .venv/bin/python scripts/backup_db.py
"""
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

# Make the project root importable so we reuse config.DB_PATH (one source of truth).
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import boto3  # noqa: E402
from config import DB_PATH  # noqa: E402  (importing config also loads .env)
# ====================================================================

BUCKET = os.environ["ATL_BACKUP_BUCKET"]
REGION = "eu-west-2"
BACKUP_DIR = ROOT / "data" / "backups"
KEEP_LOCAL = 14
# ====================================================================

def now() -> str:
  return datetime.now(timezone.utc).isoformat(timespec="seconds")


def make_copy() -> Path:
  BACKUP_DIR.mkdir(parents=True, exist_ok=True)
  stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
  dest = BACKUP_DIR / f"atl_bot_{stamp}.db"

  # mode=ro: fail loudly if the live DB is missing, instead of silently
  # creating an empty one and backing that up.
  src = sqlite3.connect(f"{Path(DB_PATH).resolve().as_uri()}?mode=ro", uri=True)
  dst = sqlite3.connect(dest)
  try:
    src.backup(dst)
  finally:
    dst.close()
    src.close()
  return dest


def check_copy(path: Path) -> None:
  conn = sqlite3.connect(path)
  try:
    result = conn.execute("PRAGMA integrity_check").fetchone()[0]
  finally:
    conn.close()
  if result != "ok":
    raise RuntimeError(f"integrity check failed on {path.name}: {result}")


def upload(path: Path) -> None:
  # No keys here: on EC2, boto3 picks up the instance role's credentials itself.
  s3 = boto3.client("s3", region_name=REGION)
  s3.upload_file(str(path), BUCKET, path.name)


def prune_local() -> None:
  copies = sorted(BACKUP_DIR.glob("atl_bot_*.db"))
  for old in copies[:-KEEP_LOCAL]:
    old.unlink()


def main() -> None:
  path = make_copy()
  check_copy(path)
  upload(path)
  prune_local()
  size_kb = path.stat().st_size // 1024
  print(f"{now()} OK  {path.name}  {size_kb} KB  -> s3://{BUCKET}/{path.name}")





# ====================================================
if __name__ == "__main__":
  try:
    main()
  except Exception as exc:
    print(f"{now()} FAILED  {exc!r}")
    sys.exit(1)