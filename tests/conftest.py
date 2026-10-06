"""Shared test setup. Settings are read from the environment when src.config
is first imported, so they're set here, before any test imports it: a
throwaway database, and dummy values for the settings CI has no .env for."""
import os
import tempfile
from pathlib import Path

import pytest

_TMP = Path(tempfile.mkdtemp(prefix="atl-tests-"))
os.environ.setdefault("BOT_TOKEN", "123:test")
os.environ.setdefault("GEMINI_API_KEY", "test")
os.environ.setdefault("ONBOARDING_GROUP_ID", "-1001")
os.environ.setdefault("MAIN_GROUP_ID", "-1002")
os.environ.setdefault("INDUCTION_GROUP_ID", "-1003")
os.environ.setdefault("OWNER_USER_ID", "9999")
os.environ["ATL_DB_PATH"] = str(_TMP / "test.db")


@pytest.fixture
def fresh_db():
  """An empty database with the full schema, for one test."""
  from src import db
  from src.config import DB_PATH
  DB_PATH.unlink(missing_ok=True)
  db.init_db()
  return db
