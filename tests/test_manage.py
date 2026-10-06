"""Tests for looking members up and expelling them (src/db/manage.py)."""
import pytest

OWNER, LEAD, ACTOR = 9999, 50, 9999


def _member(db, uid, username, status="active", first="Ada", last="Test"):
  db.upsert_member(uid, username=username, first_name=first, last_name=last)
  db.set_status(uid, status)


def test_expel_marks_removed_and_records_reason(fresh_db):
  db = fresh_db
  _member(db, 1, "ada")
  assert db.expel_member(1, ACTOR, "Scam or fraud")
  assert db.get_member(1)["status"] == db.STATUS_REMOVED
  assert db.member_summary(1)["expelled"]["note"] == "Scam or fraud"


def test_expel_twice_is_refused(fresh_db):
  db = fresh_db
  _member(db, 1, "ada")
  assert db.expel_member(1, ACTOR, "x")
  assert not db.expel_member(1, ACTOR, "x")


def test_expel_unknown_member_is_refused(fresh_db):
  assert not fresh_db.expel_member(12345, ACTOR, "x")


def test_expel_removes_every_route_back(fresh_db):
  db = fresh_db
  _member(db, 1, "AdaLead")
  db.grant_role(1, "AdaLead", "Ada", None, db.ADMIN, OWNER)
  db.grant_manager(1, OWNER)
  db.add_special(1, "AdaLead", "Ada", None, "adalead", OWNER)
  db.expel_member(1, ACTOR, "x")
  s = db.member_summary(1)
  assert s["role"] is None and not s["manager"] and not s["legacy_member"]


def test_expel_clears_a_waiting_legacy_entry(fresh_db):
  db = fresh_db
  _member(db, 1, "AdaLead")
  db.add_special_waiting("adalead", OWNER)
  db.expel_member(1, ACTOR, "x")
  assert not db.claim_special(1, "AdaLead", "Ada", None, "adalead")
  assert db.get_member(1)["status"] == db.STATUS_REMOVED


def test_expel_cancels_open_vouch_requests(fresh_db):
  db = fresh_db
  _member(db, 1, "ada")
  req = db.create_vouch_request(1, "somevouch", "profile")
  db.expel_member(1, ACTOR, "x")
  assert db.get_vouch_request(req)["status"] == "cancelled"


def test_expel_closes_open_induction_application(fresh_db):
  db = fresh_db
  _member(db, 1, "ada", status=db.STATUS_PENDING_REVIEW)
  app_id = db.create_application(1, "summary")
  db.expel_member(1, ACTOR, "x")
  app = db.get_application(app_id)
  assert app["decision"] == "declined" and app["decided_by"] == ACTOR
  # The old card's Approve button can no longer act on it.
  assert not db.decide_application(app_id, "approved", LEAD)
  assert db.get_member(1)["status"] == db.STATUS_REMOVED


def test_induction_card_refuses_an_expelled_member(fresh_db):
  import asyncio
  from types import SimpleNamespace
  from unittest.mock import AsyncMock
  from src.config import ONBOARDING_GROUP_ID
  from src.onboarding import induction
  db = fresh_db
  _member(db, 1, "ada", status=db.STATUS_REMOVED)
  app_id = db.create_application(1, "summary")   # open, as if expel missed it
  query = SimpleNamespace(
    data=f"ind:approve:{app_id}",
    message=SimpleNamespace(chat=SimpleNamespace(id=ONBOARDING_GROUP_ID)),
    from_user=SimpleNamespace(id=LEAD, first_name="Lead"),
    answer=AsyncMock(), edit_message_reply_markup=AsyncMock(),
  )
  asyncio.run(induction.handle_induction_decision(
    SimpleNamespace(callback_query=query), SimpleNamespace(bot=AsyncMock())))
  assert db.get_member(1)["status"] == db.STATUS_REMOVED
  assert db.get_application(app_id)["decision"] is None
  query.answer.assert_awaited_once()


def test_expelled_member_is_not_relinked_from_the_old_website(fresh_db):
  db = fresh_db
  _member(db, 1, "ada")
  db.expel_member(1, ACTOR, "x")
  assert not db.activate_main_group_member(1, "ada", "Ada", None, "skip")
  assert db.get_member(1)["status"] == db.STATUS_REMOVED


def test_leadership_post_does_not_reactivate_an_expelled_member(fresh_db):
  db = fresh_db
  from types import SimpleNamespace
  from src.members import roles
  _member(db, 1, "ada")
  db.expel_member(1, ACTOR, "x")
  user = SimpleNamespace(id=1, username="ada", first_name="Ada", last_name=None, is_bot=False)
  assert not roles._make_leader_admin(user)
  assert db.get_member(1)["status"] == db.STATUS_REMOVED
  assert db.get_role(1) == db.MEMBER


def test_reinstate_an_expelled_member_makes_them_active(fresh_db):
  db = fresh_db
  _member(db, 1, "ada")
  db.expel_member(1, ACTOR, "x")
  assert db.reinstate_member(1, OWNER) == db.STATUS_ACTIVE
  assert db.get_member(1)["status"] == db.STATUS_ACTIVE
  assert db.get_member(1)["status_before_removal"] is None
  assert db.member_summary(1)["expelled"] is None   # no stale "expelled" flag
  assert db.reinstate_member(1, OWNER) is None      # only removed members


@pytest.mark.parametrize("applicant_status", [
  "pending_summary", "pending_review", "awaiting_dm", "kyc_in_progress",
  "pending_access", "declined",
])
def test_reinstate_never_promotes_an_expelled_applicant(fresh_db, applicant_status):
  db = fresh_db
  _member(db, 1, "applicant", status=applicant_status)
  db.expel_member(1, ACTOR, "x")
  assert db.get_member(1)["status_before_removal"] == applicant_status
  assert db.reinstate_member(1, OWNER) == db.STATUS_PENDING_SUMMARY
  assert db.get_member(1)["status"] == db.STATUS_PENDING_SUMMARY


def test_removal_noticed_in_telegram_is_recorded_and_reinstated(fresh_db):
  db = fresh_db
  _member(db, 1, "ada")
  db.set_status(1, db.STATUS_REMOVED, note="removed from the main group in Telegram")
  assert db.get_member(1)["status_before_removal"] == db.STATUS_ACTIVE
  assert db.reinstate_member(1, OWNER) == db.STATUS_ACTIVE


def test_removed_before_this_was_recorded_counts_as_active(fresh_db):
  db = fresh_db
  _member(db, 1, "ada")
  with db.get_conn() as conn:   # as rows removed before the column existed
    conn.execute("UPDATE members SET status = 'removed', status_before_removal = NULL "
                 "WHERE user_id = 1")
  assert db.reinstate_member(1, OWNER) == db.STATUS_ACTIVE


def test_setting_removed_twice_keeps_the_original_status(fresh_db):
  db = fresh_db
  _member(db, 1, "applicant", status="kyc_in_progress")
  db.set_status(1, db.STATUS_REMOVED)
  db.set_status(1, db.STATUS_REMOVED)
  assert db.get_member(1)["status_before_removal"] == "kyc_in_progress"


def test_existing_database_gets_the_new_column(fresh_db):
  db = fresh_db
  with db.get_conn() as conn:
    conn.execute("ALTER TABLE members DROP COLUMN status_before_removal")
  db.init_db()
  with db.get_conn() as conn:
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(members)")}
  assert "status_before_removal" in cols


def test_managers_grant_list_revoke(fresh_db):
  db = fresh_db
  _member(db, LEAD, "obtlead")
  assert db.grant_manager(LEAD, OWNER)
  assert not db.grant_manager(LEAD, OWNER)
  assert db.is_manager(LEAD)
  assert [r["username"] for r in db.list_managers()] == ["obtlead"]
  assert db.revoke_manager(LEAD, OWNER)
  assert not db.is_manager(LEAD)


def test_only_active_members_can_be_made_leads(fresh_db):
  db = fresh_db
  _member(db, 1, "expelled")
  db.expel_member(1, ACTOR, "x")
  _member(db, 2, "applicant", status=db.STATUS_PENDING_REVIEW)
  for uid, status in ((1, db.STATUS_REMOVED), (2, db.STATUS_PENDING_REVIEW)):
    assert not db.grant_manager(uid, OWNER)
    assert not db.is_manager(uid)
    assert db.get_member(uid)["status"] == status   # status left alone
  assert not db.grant_manager(12345, OWNER)          # not on record


def test_lead_removed_outside_expel_loses_access(fresh_db):
  db = fresh_db
  _member(db, LEAD, "obtlead")
  db.grant_manager(LEAD, OWNER)
  # e.g. banned from the main group in Telegram (members/main_group.py)
  db.set_status(LEAD, db.STATUS_REMOVED, note="removed from the main group in Telegram")
  assert not db.is_manager(LEAD)


def test_search_by_name_username_and_kyc_name(fresh_db):
  db = fresh_db
  _member(db, 1, "ada_ok", first="Ada", last="Okafor")
  _member(db, 2, None, first="Bola", last="Test")
  db.save_kyc_answer(2, "full_name", "Bolanle Adeyemi")
  assert [m["user_id"] for m in db.search_members("okafor")] == [1]
  assert [m["user_id"] for m in db.search_members("ADA_OK")] == [1]
  assert [m["user_id"] for m in db.search_members("adeyemi")] == [2]
  assert db.search_members("nobody") == []


def test_only_the_owner_may_expel_privileged_members(fresh_db):
  db = fresh_db
  from src.members import manage
  _member(db, LEAD, "obtlead")
  db.grant_manager(LEAD, OWNER)
  _member(db, 2, "plain")
  _member(db, 3, "anadmin")
  db.grant_role(3, "anadmin", "A", None, db.ADMIN, OWNER)
  assert manage._may_expel(LEAD, 2)[0]            # lead -> ordinary member
  assert not manage._may_expel(LEAD, 3)[0]        # lead -> admin
  assert not manage._may_expel(LEAD, OWNER)[0]    # anyone -> owner
  assert not manage._may_expel(LEAD, LEAD)[0]     # yourself
  assert manage._may_expel(OWNER, 3)[0]           # owner -> admin


def test_who_can_manage(fresh_db):
  db = fresh_db
  from src.members import manage
  _member(db, LEAD, "obtlead")
  _member(db, 3, "anadmin")
  db.grant_role(3, "anadmin", "A", None, db.ADMIN, OWNER)
  db.grant_manager(LEAD, OWNER)
  assert manage.can_manage(OWNER)
  assert manage.can_manage(LEAD)
  assert not manage.can_manage(3)   # admins don't get members' data


# --- only /reinstate brings someone back -----------------------------------
def _user(uid, username):
  from types import SimpleNamespace
  return SimpleNamespace(id=uid, user_id=uid, username=username,
                         first_name="Ada", last_name=None, is_bot=False)


def _expelled(db, uid=1, username="ada", status="active"):
  _member(db, uid, username, status=status)
  db.expel_member(uid, ACTOR, "x")


def test_grant_role_refuses_an_expelled_member(fresh_db):
  db = fresh_db
  _expelled(db)
  assert not db.grant_role(1, "ada", "Ada", None, db.ADMIN, OWNER)
  assert db.get_member(1)["status"] == db.STATUS_REMOVED
  assert db.get_role(1) == db.MEMBER


def test_add_special_refuses_an_expelled_member(fresh_db):
  db = fresh_db
  _expelled(db)
  assert not db.add_special(1, "ada", "Ada", None, "ada", OWNER)
  assert db.get_member(1)["status"] == db.STATUS_REMOVED
  assert not db.member_summary(1)["legacy_member"]


def test_waiting_legacy_entry_does_not_bring_back_an_expelled_member(fresh_db):
  db = fresh_db
  _expelled(db)
  db.add_special_waiting("ada", OWNER)   # added after the expulsion
  assert not db.claim_special(1, "ada", "Ada", None, "ada")
  assert db.get_member(1)["status"] == db.STATUS_REMOVED
  assert db.is_special_waiting("ada")    # still there if they're reinstated


def _owner_command(module, handler, args=None, picked=None):
  """Run an owner command or picker answer; return the reply text."""
  import asyncio
  from types import SimpleNamespace
  from unittest.mock import AsyncMock
  from telegram.ext import ApplicationHandlerStop
  reply = AsyncMock()
  msg = SimpleNamespace(reply_text=reply,
                        users_shared=SimpleNamespace(users=picked or []))
  update = SimpleNamespace(effective_user=SimpleNamespace(id=OWNER),
                           message=msg, effective_message=msg)
  try:
    asyncio.run(getattr(module, handler)(update, SimpleNamespace(args=args or [])))
  except ApplicationHandlerStop:
    pass
  return reply.await_args.args[0]


@pytest.mark.parametrize("command", ["roles", "special"])
def test_owner_commands_refuse_an_expelled_member(fresh_db, command):
  import importlib
  db = fresh_db
  module = importlib.import_module(f"src.members.{command}")
  name = "cmd_admin" if command == "roles" else "cmd_legacy"
  _expelled(db, username="adalead")   # usernames need 5+ characters
  typed = _owner_command(module, name, args=["@adalead"])
  picked = _owner_command(module, "handle_picked", picked=[_user(1, "adalead")])
  for text in (typed, picked):
    assert "Use /reinstate first" in text and "@adalead" in text
  assert db.get_member(1)["status"] == db.STATUS_REMOVED
  assert db.get_role(1) == db.MEMBER
  assert not db.member_summary(1)["legacy_member"]


def test_owner_adding_them_by_hand_clears_the_old_status(fresh_db):
  db = fresh_db
  _expelled(db, status=db.STATUS_PENDING_REVIEW)
  db.activate_by_owner(1, "ada", "Ada", None)
  member = db.get_member(1)
  assert member["status"] == db.STATUS_ACTIVE
  assert member["status_before_removal"] is None
