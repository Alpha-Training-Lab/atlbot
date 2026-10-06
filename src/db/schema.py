"""Every table, in one place. init_db() runs this at startup; each statement
is IF NOT EXISTS, so it is safe to run against an existing database.

SQLite is the single source of truth. Excel is a generated export.
"""
# --- status values ---------------------------------------------------
STATUS_PENDING_SUMMARY = "pending_summary"
STATUS_PENDING_REVIEW  = "pending_review"
STATUS_DECLINED        = "declined"
STATUS_AWAITING_DM     = "awaiting_dm"
STATUS_KYC_IN_PROGRESS = "kyc_in_progress"
STATUS_PENDING_ACCESS  = "pending_access"
STATUS_ACTIVE          = "active"
STATUS_REMOVED         = "removed"

ALL_STATUSES = (
  STATUS_PENDING_SUMMARY, STATUS_PENDING_REVIEW, STATUS_DECLINED,
  STATUS_AWAITING_DM, STATUS_KYC_IN_PROGRESS, STATUS_PENDING_ACCESS,
  STATUS_ACTIVE, STATUS_REMOVED,
)

_STATUS_SQL_LIST = ", ".join(f"'{s}'" for s in ALL_STATUSES)

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS members (
    user_id         INTEGER PRIMARY KEY,
    username        TEXT,
    first_name      TEXT,
    last_name       TEXT,
    status          TEXT NOT NULL DEFAULT '{STATUS_PENDING_SUMMARY}'
                    CHECK (status IN ({_STATUS_SQL_LIST})),
    kyc_field_index INTEGER NOT NULL DEFAULT 0,
    kyc_attempts    INTEGER NOT NULL DEFAULT 0,
    first_seen      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_members_status   ON members(status);
CREATE INDEX IF NOT EXISTS idx_members_username ON members(username);

CREATE TABLE IF NOT EXISTS applications (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id           INTEGER NOT NULL REFERENCES members(user_id),
    summary_text      TEXT,
    source_chat_id    INTEGER,
    source_message_id INTEGER,
    decision          TEXT CHECK (decision IN ('approved', 'declined')),
    decided_by        INTEGER,
    decided_at        TEXT,
    created_at        TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_applications_user ON applications(user_id);

CREATE TABLE IF NOT EXISTS member_events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       INTEGER NOT NULL REFERENCES members(user_id),
    event         TEXT NOT NULL,
    actor_user_id INTEGER,
    note          TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_events_user ON member_events(user_id);

CREATE TABLE IF NOT EXISTS kyc_responses (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER NOT NULL REFERENCES members(user_id),
    field_key    TEXT NOT NULL,
    value_text   TEXT,
    file_ref     TEXT,
    needs_review INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(user_id, field_key)
);

CREATE TABLE IF NOT EXISTS scheduled_deletions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id    INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    delete_at  TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_deletions_due
  ON scheduled_deletions(delete_at);

-- The induction-group "you've been approved, tap to register" post, kept so
-- it can be deleted the moment the member starts registering.
CREATE TABLE IF NOT EXISTS registration_prompts (
    user_id    INTEGER PRIMARY KEY REFERENCES members(user_id),
    chat_id    INTEGER NOT NULL,
    message_id INTEGER NOT NULL
);


CREATE TABLE IF NOT EXISTS legacy_members (
    legacy_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    source_row     INTEGER NOT NULL,
    username_key   TEXT,
    phone_key      TEXT,
    linked_user_id INTEGER UNIQUE REFERENCES members(user_id),
    linked_at      TEXT,
    imported_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_legacy_username
  ON legacy_members(username_key);

CREATE INDEX IF NOT EXISTS idx_legacy_phone
  ON legacy_members(phone_key);

CREATE TABLE IF NOT EXISTS legacy_responses (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    legacy_id    INTEGER NOT NULL REFERENCES legacy_members(legacy_id),
    field_key    TEXT NOT NULL,
    value_text   TEXT,
    needs_review INTEGER NOT NULL DEFAULT 0,
    UNIQUE(legacy_id, field_key)
);

-- A member filling in missing profile details. Kept apart from members.status
-- so an active member who stops halfway is still active.
CREATE TABLE IF NOT EXISTS profile_sessions (
    user_id         INTEGER PRIMARY KEY REFERENCES members(user_id),
    field_keys      TEXT NOT NULL,
    position        INTEGER NOT NULL DEFAULT 0,
    attempts        INTEGER NOT NULL DEFAULT 0,
    card_message_id INTEGER,
    started_at      TEXT NOT NULL DEFAULT (datetime('now')),
    last_active_at  TEXT          -- last answer; idle sessions are closed
);

-- Profile details held until an admin approves them.
CREATE TABLE IF NOT EXISTS pending_changes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER NOT NULL REFERENCES members(user_id),
    field_key       TEXT NOT NULL,
    value_text      TEXT,
    file_ref        TEXT,
    card_message_id INTEGER,
    requested_at    TEXT NOT NULL DEFAULT (datetime('now')),
    decision        TEXT CHECK (decision IN ('approved', 'rejected')),
    decided_by      INTEGER,
    decided_by_name TEXT,
    decided_at      TEXT
);

-- At most one undecided request per member per field.
CREATE UNIQUE INDEX IF NOT EXISTS idx_pending_one_open
  ON pending_changes(user_id, field_key) WHERE decision IS NULL;

CREATE INDEX IF NOT EXISTS idx_pending_card
  ON pending_changes(card_message_id);

-- The current one-time main-group invite for an approved member who isn't
-- in the group, so repeat messages re-send it instead of minting new ones.
CREATE TABLE IF NOT EXISTS main_group_invites (
    user_id     INTEGER PRIMARY KEY REFERENCES members(user_id),
    invite_link TEXT NOT NULL,
    expires_at  TEXT NOT NULL
);

-- When someone not on record was last asked, in the main group, to update
-- their details. Keyed by Telegram id: they may have no members row yet.
CREATE TABLE IF NOT EXISTS group_prompts (
    user_id     INTEGER PRIMARY KEY,
    prompted_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- A member editing details already on file. draft holds the new values
-- (JSON) until they confirm; nothing touches kyc_responses before that.
CREATE TABLE IF NOT EXISTS edit_sessions (
    user_id    INTEGER PRIMARY KEY REFERENCES members(user_id),
    group_key  TEXT NOT NULL,
    position   INTEGER NOT NULL DEFAULT 0,
    attempts   INTEGER NOT NULL DEFAULT 0,
    draft          TEXT NOT NULL DEFAULT '{{}}',
    started_at     TEXT NOT NULL DEFAULT (datetime('now')),
    last_active_at TEXT          -- last answer; idle sessions are closed
);

-- A member's request for their vouch to confirm them. The vouch is known
-- by username until they contact Alpha, which reveals their Telegram id.
CREATE TABLE IF NOT EXISTS vouch_requests (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER NOT NULL REFERENCES members(user_id),
    vouch_key       TEXT NOT NULL,
    vouch_user_id   INTEGER,
    purpose         TEXT NOT NULL CHECK (purpose IN ('registration', 'profile')),
    card_message_id INTEGER,
    status          TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'yes', 'no', 'expired',
                                      'not_member', 'cancelled')),
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    last_nudged_at  TEXT,
    decided_at      TEXT
);

CREATE INDEX IF NOT EXISTS idx_vouch_open
  ON vouch_requests(vouch_key, status);

-- An admin's "Other" decline prompt, so their typed reply can be matched to
-- the member without the member's id appearing in the prompt text.
CREATE TABLE IF NOT EXISTS reason_prompts (
    message_id      INTEGER PRIMARY KEY,
    user_id         INTEGER NOT NULL,
    card_message_id INTEGER NOT NULL,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

-- The owner's hand-picked "Legacy Members": special individuals who are
-- active without onboarding. user_id is NULL until Alpha knows who they are
-- (added by typed username and not yet seen). Not to be confused with
-- legacy_members, the old website's spreadsheet backlog.
CREATE TABLE IF NOT EXISTS special_members (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER UNIQUE REFERENCES members(user_id),
    username_key TEXT,
    added_by     INTEGER,
    added_at     TEXT NOT NULL DEFAULT (datetime('now')),
    linked_at    TEXT
);

-- One waiting entry per typed username.
CREATE UNIQUE INDEX IF NOT EXISTS idx_special_waiting
  ON special_members(username_key) WHERE user_id IS NULL;

-- Members with a role beyond ordinary member. No row = ordinary member.
-- Roles are checked in code (db/roles.py ROLES), so adding one later needs
-- no change here. The owner (OWNER_USER_ID) is above roles and not listed.
-- source: 'owner' (granted with /admin; stays until the owner removes it)
-- or 'leadership' (from being in the leadership group; goes when they leave).
CREATE TABLE IF NOT EXISTS member_roles (
    user_id    INTEGER PRIMARY KEY REFERENCES members(user_id),
    role       TEXT NOT NULL,
    source     TEXT NOT NULL DEFAULT 'owner',
    granted_by INTEGER,
    granted_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ----- Weekly brief (src/brief/) ---------------------------------------
-- Groups Alpha is in. Only enabled groups have their messages stored.
-- config.BRIEF_NEVER_READ (leadership, onboarding, induction) is never
-- read, whatever this table says.
CREATE TABLE IF NOT EXISTS brief_groups (
    chat_id  INTEGER PRIMARY KEY,
    enabled  INTEGER NOT NULL DEFAULT 0,
    added_by INTEGER,
    added_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Raw group messages. Deleted as soon as their day has been digested.
CREATE TABLE IF NOT EXISTS brief_messages (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id          INTEGER NOT NULL,
    message_id       INTEGER NOT NULL,
    user_id          INTEGER NOT NULL,
    reply_to_user_id INTEGER,      -- NULL unless it counts toward "most helpful"
    text             TEXT NOT NULL,
    sent_at          TEXT NOT NULL, -- UTC, 'YYYY-MM-DD HH:MM:SS'
    UNIQUE(chat_id, message_id)
);

CREATE INDEX IF NOT EXISTS idx_brief_messages_sent
  ON brief_messages(sent_at);

-- One row per group per finished UTC day: counts, plus Gemini's digest
-- (NULL if Gemini kept failing). Deleted once that week's brief is sent.
-- replies_json maps user_id to the number of replies their messages got;
-- senders_json lists who posted that day.
CREATE TABLE IF NOT EXISTS brief_days (
    day           TEXT NOT NULL,   -- 'YYYY-MM-DD', UTC
    chat_id       INTEGER NOT NULL,
    message_count INTEGER NOT NULL,
    replies_json  TEXT NOT NULL,
    senders_json  TEXT NOT NULL DEFAULT '[]',
    digest_json   TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (day, chat_id)
);

-- People wished a happy birthday in the felicitation group, taken from the
-- @mentions in those messages. No message text is stored. celebrant is
-- 'id:<telegram id>' or 'u:<username, lowercase>'. Deleted with the week.
CREATE TABLE IF NOT EXISTS brief_birthdays (
    day       TEXT NOT NULL,       -- 'YYYY-MM-DD', UTC
    celebrant TEXT NOT NULL,
    PRIMARY KEY (day, celebrant)
);

-- One row per weekly brief, so a restart can never post one twice.
-- week_start is the Monday the reported week began.
CREATE TABLE IF NOT EXISTS brief_weeks (
    week_start TEXT PRIMARY KEY,
    status     TEXT NOT NULL CHECK (status IN ('sending', 'sent')),
    used_llm   INTEGER NOT NULL DEFAULT 0,
    message_id INTEGER,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    sent_at    TEXT
);

-- People the owner trusts to look members up and expel them (the onboarding
-- lead). Separate from roles: the leadership group makes people admins
-- automatically, and that must never hand out access to members' data.
CREATE TABLE IF NOT EXISTS member_managers (
    user_id    INTEGER PRIMARY KEY REFERENCES members(user_id),
    granted_by INTEGER,
    granted_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""
