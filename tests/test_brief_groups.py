"""Which groups the weekly brief reads (src/brief/capture.py)."""
import asyncio

from telegram import Bot, Update

from src.brief import capture

OWNER = 9999          # OWNER_USER_ID in conftest.py
STRANGER = 4242
_bot = Bot("123:test")


def _change(chat_id, by, old, new):
  alpha = {"id": 555, "is_bot": True, "first_name": "Alpha"}
  status = {"left": {"status": "left"}, "member": {"status": "member"},
            "administrator": {"status": "administrator", "can_be_edited": False,
                              "is_anonymous": False, "can_manage_chat": True,
                              "can_delete_messages": True, "can_manage_video_chats": True,
                              "can_restrict_members": True, "can_promote_members": False,
                              "can_change_info": True, "can_invite_users": True,
                              "can_post_stories": False, "can_edit_stories": False,
                              "can_delete_stories": False}}
  return Update.de_json({"update_id": 1, "my_chat_member": {
    "chat": {"id": chat_id, "type": "supergroup", "title": "G"},
    "from": {"id": by, "is_bot": False, "first_name": "X"}, "date": 0,
    "old_chat_member": {"user": alpha, **status[old]},
    "new_chat_member": {"user": alpha, **status[new]}}}, _bot)


def _track(update):
  asyncio.run(capture.track_alpha_groups(update, None))


def test_added_by_owner_is_read(fresh_db):
  _track(_change(-500, OWNER, "left", "member"))
  assert fresh_db.brief_group_enabled(-500)


def test_added_by_stranger_is_not_read(fresh_db):
  _track(_change(-501, STRANGER, "left", "member"))
  assert not fresh_db.brief_group_enabled(-501)


def test_promoted_by_owner_in_a_group_alpha_was_already_in(fresh_db):
  _track(_change(-502, OWNER, "member", "administrator"))
  assert fresh_db.brief_group_enabled(-502)


def test_stranger_promoting_alpha_doesnt_switch_a_group_off(fresh_db):
  _track(_change(-503, OWNER, "left", "member"))
  _track(_change(-503, STRANGER, "member", "administrator"))
  assert fresh_db.brief_group_enabled(-503)


def test_seeded_groups_are_read_but_never_read_groups_stay_off(fresh_db, monkeypatch):
  never = next(iter(capture.BRIEF_NEVER_READ))
  monkeypatch.setattr(capture, "BRIEF_READ_GROUPS", [-600, -601, never])
  fresh_db.set_brief_group(-601, False, STRANGER)   # once added by a stranger
  capture.seed_groups()
  assert fresh_db.brief_group_enabled(-600)
  assert fresh_db.brief_group_enabled(-601)
  assert not fresh_db.brief_group_enabled(never)


def test_leaving_forgets_the_group(fresh_db):
  _track(_change(-504, OWNER, "left", "administrator"))
  _track(_change(-504, OWNER, "administrator", "left"))
  assert not fresh_db.brief_group_enabled(-504)
