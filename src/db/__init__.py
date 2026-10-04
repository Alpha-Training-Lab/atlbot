"""The data layer. Code elsewhere does `from src import db` and calls db.<name>;
this file is the index of everything available, grouped by the module that
holds it."""
from src.db.connection import get_conn, init_db
from src.db.schema import (
  ALL_STATUSES, STATUS_ACTIVE, STATUS_AWAITING_DM, STATUS_DECLINED,
  STATUS_KYC_IN_PROGRESS, STATUS_PENDING_ACCESS, STATUS_PENDING_REVIEW,
  STATUS_PENDING_SUMMARY, STATUS_REMOVED,
)
from src.db.members import (
  count_events, find_member_by_username, get_member, refresh_telegram_details, log_event, seconds_since_last_event, set_status,
  upsert_member,
)
from src.db.onboarding import (
  advance_kyc, bump_kyc_attempts, create_application, decide_application,
  get_application, get_kyc_answers, pop_registration_prompt, save_kyc_answer,
  save_registration_prompt,
  pop_reason_prompt, save_reason_prompt,
)
from src.db.deletions import clear_deletion, due_deletions, schedule_deletion
from src.db.legacy import (
  activate_main_group_member, find_legacy_match, is_legacy_linked, link_legacy,
  phone_key, username_key,
)
from src.db.profile import (
  add_pending_change, advance_profile_session, attach_change_to_card,
  bump_edit_attempts, bump_profile_attempts, decide_card_changes, decide_change,
  decide_changes,
  end_edit_session, end_profile_session, get_card_changes, get_change,
  get_edit_session, get_open_changes, get_profile_session, save_edit_draft,
  set_profile_card, start_edit_session, start_profile_session,
  end_edit_session_if_idle, end_profile_session_if_idle, idle_edit_sessions,
  idle_profile_sessions,
)
from src.db.main_group import (
  activate_by_owner, clear_invite, get_invite, mark_prompted, save_invite,
  should_prompt,
)
from src.db.vouch import (
  cancel_vouch_requests, create_vouch_request, decide_vouch_request,
  get_vouch_request, latest_vouch_request, mark_vouch_nudged,
  open_requests_for_vouch, open_vouch_requests_with_age, set_vouch_contact,
  username_on_record,
)

__all__ = [
  "get_conn", "init_db", "ALL_STATUSES", "STATUS_ACTIVE",
  "STATUS_AWAITING_DM", "STATUS_DECLINED", "STATUS_KYC_IN_PROGRESS",
  "STATUS_PENDING_ACCESS", "STATUS_PENDING_REVIEW",
  "STATUS_PENDING_SUMMARY", "STATUS_REMOVED", "count_events", "get_member",
  "log_event", "seconds_since_last_event", "set_status", "upsert_member",
  "advance_kyc", "bump_kyc_attempts", "create_application",
  "decide_application", "get_application", "get_kyc_answers",
  "pop_registration_prompt", "save_kyc_answer", "save_registration_prompt",
  "clear_deletion", "due_deletions", "schedule_deletion",
  "activate_main_group_member", "find_legacy_match", "is_legacy_linked",
  "link_legacy", "phone_key", "username_key", "add_pending_change",
  "advance_profile_session", "attach_change_to_card", "bump_edit_attempts",
  "bump_profile_attempts", "decide_card_changes", "decide_change",
  "end_edit_session", "end_profile_session", "get_card_changes",
  "get_change", "get_edit_session", "get_open_changes",
  "get_profile_session", "save_edit_draft", "set_profile_card",
  "start_edit_session", "start_profile_session", "activate_by_owner",
  "clear_invite", "get_invite", "mark_prompted", "save_invite",
  "should_prompt", "find_member_by_username", "decide_changes",
  "cancel_vouch_requests", "create_vouch_request", "decide_vouch_request",
  "get_vouch_request", "latest_vouch_request", "mark_vouch_nudged",
  "open_requests_for_vouch", "open_vouch_requests_with_age",
  "pop_reason_prompt", "save_reason_prompt", "set_vouch_contact", "username_on_record", "refresh_telegram_details",
  "end_edit_session_if_idle", "end_profile_session_if_idle",
  "idle_edit_sessions", "idle_profile_sessions",
]
