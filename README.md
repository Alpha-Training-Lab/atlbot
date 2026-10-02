# Alpha — ATL Onboarding & Community Bot

Alpha is the Telegram bot for **Alpha Training Lab (ATL)**. It runs the
member journey end to end:

- **Onboarding new members:** welcoming arrivals in the induction group,
  gating how long they must spend with the induction material, telling a
  genuine question apart from a readiness signal, routing applications to
  admins, walking approved members through identity verification (KYC), and
  sending a single-use invite into the main group.
- **Looking after existing members:** recognising members imported from the
  old website, keeping the main group and the members database in step
  (removing people who skipped onboarding), and letting members view,
  complete and edit their profile, with admin approval for identity details.
- **Answering questions:** a Gemini-backed assistant for anything else
  members ask in a DM.

This document describes the system **as the code currently behaves**.

## Contents

- [Architecture](#architecture)
- [The member lifecycle](#the-member-lifecycle)
- [How it works](#how-it-works)
  - [Onboarding](#onboarding-srconboarding)
  - [Existing members](#existing-members-srcmembers)
  - [Alpha, the assistant](#alpha-the-assistant-srcassistant)
  - [Scheduled message cleanup](#scheduled-message-cleanup-srccommoncleanuppy)
- [Project structure](#project-structure)
- [Getting started](#getting-started)
- [Environment variables](#environment-variables)
- [Running the bot](#running-the-bot)
- [Database](#database)
- [Bot commands](#bot-commands)
- [Scripts](#scripts)
- [Editing the KYC form](#editing-the-kyc-form)
- [Known limitations](#known-limitations)

## Architecture

The code is organised by feature. Each feature is a package under `src/`,
and they all share one data layer:

```
                        main.py  (wires every handler, in priority order)
                           │
        ┌──────────────────┼───────────────────────┐
        ▼                  ▼                       ▼
  src/onboarding/     src/members/           src/assistant/
  new members         existing members       Alpha, the LLM
        │                  │                       │
        └──────┬───────────┴───────────┬───────────┘
               ▼                       ▼
         src/kyc_form/            src/common/
         questions +              Telegram helpers,
         validation               cleanup job
               │                       │
               └───────────┬───────────┘
                           ▼
                       src/db/    (SQLite: the single source of truth)
                           │
                     src/config.py (settings from .env)
```

Dependencies only point downwards in this picture, with two deliberate
sideways links:

- `onboarding` uses `members/main_group.py` for main-group invite links and
  the "membership removed" message.
- `assistant/chat.py` uses `members/` to keep the main group in step before
  answering.

`members/` never imports `onboarding/` or `assistant/`.

**Who gets a message first.** `main.py` registers every handler. Telegram
updates pass through handler groups in order: −1, then 0, then 1. Within a
group, only the first matching handler runs, and a handler can stop an update
from reaching later groups. So the order in `main.py` is the bot's priority
list:

| Group | Handler | Claims |
|------:|---------|--------|
| −1 | `members/legacy.py` | Shared phone numbers and Skip taps from the link flow (stopped there, so they never reach KYC or the LLM); silent username matching on main-group posts and DMs |
| 0 | `src/commands.py`, `members/profile.py` | `/start`, `/profile`, `/kyc`, `/chatid` |
| 0 | `onboarding/induction.py` | Everything in the induction group |
| 0 | `members/profile.py`, `members/profile_edit.py` | DMs from a member part-way through filling in or editing their profile |
| 0 | `onboarding/kyc.py` | DMs from a member part-way through registration |
| 0 | `onboarding/access_review.py` | Admin buttons and typed decline reasons in the onboarding group |
| 0 | `members/main_group.py` | Joins, leaves and removals in the main group |
| 1 | `assistant/chat.py` | Any private text nothing above claimed |

The profile collectors must stay above the KYC collector: the KYC collector
matches every private message and only then checks the member's status.

## The member lifecycle

Every member has exactly one `status` in the `members` table, and that value
drives what the bot says to them and which buttons it shows:

```
pending_summary  →  pending_review  →  awaiting_dm  →  kyc_in_progress
                                                             │
                                                             ▼
                                                       pending_access
                                                        ╱          ╲
                                                   active          declined
                                                     │          (back to kyc_in_progress)
                                                     ▼
                                                  removed
```

- **`pending_summary`**: the default for every new row. The member is still
  reading in the induction group, or an admin said "not yet".
- **`pending_review`**: the member posted their "I'm ready" message, tagging
  the bot and the required admins; a review card is waiting in the
  onboarding group.
- **`awaiting_dm`**: an admin approved the induction post. The member has
  been told to DM the bot to begin registration.
- **`kyc_in_progress`**: the member is part-way through the KYC questions;
  `kyc_field_index` tracks which one.
- **`pending_access`**: every active KYC question is answered; a review
  card with all the answers is waiting in the onboarding group.
- **`active`**: a full member. Reached by admin approval, by linking an
  old-website record, by being verified in the main group, or by the owner
  adding them to the main group directly.
- **`declined`**: an admin declined the registration, with a reason. Below
  `MAX_DECLINES` (3) the member can register again straight away; from then
  on there is a `COOLDOWN_SECONDS` (6 hour) wait after each decline.
- **`removed`**: the member was banned from the main group. Alpha tells
  them their membership has been removed and to contact an admin, in DMs,
  in the induction group and on `/kyc`.

## How it works

### Onboarding (`src/onboarding/`)

#### 1. Induction (`induction.py`)

Everything that happens in the **induction group** (`INDUCTION_GROUP_ID`)
before a member is handed to registration.

**On join** (`handle_new_member`): the bot creates the member row, logs a
`joined_induction` event (used later to measure time in the group), and posts
`WELCOME_INDUCTION`, deleted after `WELCOME_DELETE_SECONDS` (24h).

**Catching the tag** (`handle_induction_post`): the bot only reacts when it
is @-mentioned or replied to. Then:

1. **Status short-circuits.** A member already under review, approved,
   further along, or removed gets a short status reply instead. The reply and
   their post are deleted together after `REMINDER_DELETE_SECONDS` (3h).
2. **Tag check** (`_missing_tags`). The post must mention every handle in
   `REQUIRED_TAGS`, matched on word boundaries so `@Dr_evidence` doesn't
   match `@Dr_evidence2`.
3. **Intent classification.** Only if tags are missing, Gemini
   (`classify_induction_intent`) decides whether the post is a **question**
   or an **onboarding** signal.
4. **Question path.** Answered publicly by `ask_alpha`, told to keep it to
   three sentences and never post an invite link.
5. **Minimum time.** If the member joined less than `MIN_INDUCTION_SECONDS`
   ago (10 days by default), they're told roughly how long is left. Members
   who joined before the bot was running have no `joined_induction` event
   and skip this gate.
6. **Incomplete tags.** An onboarding post with tags missing gets
   `INDUCTION_POST_INCOMPLETE`.
7. **Valid submission.** An application is recorded, status moves to
   `pending_review`, and `send_summary_card` posts a review card to the
   onboarding group with a link to the post.

**The decision** (`handle_induction_decision`, buttons `ind:`): ✅ Approve or
❌ Not yet. `db.decide_application` is atomic, so two admins tapping at once
can't double-process one application. The member's post is deleted either
way. On approve, status becomes `awaiting_dm` and the member gets a public
"Start registration" button (`?start=kyc`); that post is deleted as soon as
they start registering, or after 24h. On "not yet", status returns to
`pending_summary` and the member is pointed back to the material.

#### 2. Registration, KYC (`kyc.py`)

Entered through the `?start=kyc` deep link (or `/kyc`).
`handle_kyc_entry` branches on status: start fresh, resume, restart after a
decline (subject to the retry rule in `seconds_until_retry`), or explain
why there's nothing to do.

Questions come one at a time from `active_fields()` in
`src/kyc_form/fields.py`. Each answer is checked by
`src/kyc_form/validators.py` before it's saved. Choice questions use buttons
(`kyc:`); document questions need a photo or file. After three bad attempts
on one question the answer is saved with `needs_review=1` and the flow moves
on, so nobody gets stuck. When every question is answered, `finish_kyc` sets
`pending_access` and hands over to `access_review.py`.

#### 3. Admin access review (`access_review.py`)

`build_kyc_card` posts every answer to the onboarding group (birthdays
spelled out, e.g. "5 July", so `07-05` can't be misread as 7 May), with the
member's ID photos posted as replies under the card, and **Approve /
Decline** buttons (`acc:`).

- **Approve** sets `active` and DMs the member `welcome_approved()` with a
  single-use invite link from `members/main_group.invite_for`. The link is
  stored, so the member never ends up with two different ones. If the invite
  can't be created, the member is told an admin will follow up instead.
- **Decline** opens a tick-box reason picker (`DECLINE_REASONS`) with an
  "Other" option, which asks the admin to reply with a typed reason.
  `_finalise_decline` sets `declined`, tells the member (with a "Start
  registration again" button) and marks the card.
- If the member can't be messaged (for example, they blocked the bot), a
  notice is posted under the card so an admin can reach them another way.

### Existing members (`src/members/`)

#### 4. Keeping the main group in step (`main_group.py`)

The rule: everyone in the main group is on record, and every approved member
is in the main group. The Bot API can't list a group's members, so this is
enforced whenever Alpha sees someone.

- **Joins** (`handle_main_group_member_update`, needs Alpha to be an admin in
  the main group). Someone joining without an `active` record is removed
  (ban, then unban, so they can come back properly). If Alpha can, it tells
  them where to start (`ONBOARDING_ENTRY_URL`), and it alerts the owner with
  how they got in. People the owner adds, or who join through the owner's
  links, are recorded as `active` with an empty profile. **With
  `OWNER_USER_ID` unset, nobody is removed.**
- **Leaves.** A member banned from the group becomes `removed`. A member who
  left on their own stays `active`, and Alpha offers a fresh one-time invite
  the next time they message.
- `main_group_state` asks Telegram whether someone is in the group, cached
  for 10 minutes; `in_main_group` always asks fresh, for decisions like
  recognising a member.

#### 5. Members from the old website (`legacy.py`)

Members imported from the old website's spreadsheet (`scripts/import_legacy.py`)
are recognised instead of being sent through induction:

- **Passively.** The first post Alpha sees from someone in the main group
  (or a DM) is matched against the spreadsheet's Telegram usernames.
  Main-group posters still not on record get a short in-group nudge, at most
  weekly, deleted after 15 minutes.
- **One tap.** `?start=link` (the "I'm already an ATL member" button) checks
  they're in the main group, then offers **Share my number** or **Skip**. A
  matching phone links their old record and copies its answers into their
  profile. No match, or Skip, still makes a verified main-group member
  `active`, with an empty profile. The number is used once for matching and
  is never stored, logged, or passed to KYC or the LLM.

#### 6. Profile (`profile.py`)

`/profile`, the **My profile** button under Alpha's replies, or a plain
`/start` shows an active member each detail as done ✅, waiting for approval
⏳, needing an update ⚠️ or missing ❌. The message deletes itself after 10
minutes.

**Fill in missing details** asks the gaps one at a time. Fields marked
`"edit": "self"` in `src/kyc_form/fields.py` save straight away. Identity and
vouch details go to a card in the onboarding group, where admins approve or
reject each one before it's saved. The member is told the outcome once
everything on the card is decided.

#### 7. Editing details on file (`profile_edit.py`)

**Edit my details** opens a menu. Vouch name and username, and ID type plus
both ID photos, are edited together, so an ID record is never half-changed.
Every edit shows current → new and waits for the member to confirm.
Self-edit fields save on confirm. Approval fields go to the onboarding group
as one card, approved or rejected as a whole (a rejection carries a preset
reason the member sees). Name and birthday changes post the member's current
ID under the card to compare against.

### Alpha, the assistant (`src/assistant/`)

`chat.py` handles any private text nothing else claimed. Before involving the
LLM it keeps the main group in step: removed members are told so, main-group
members with no record are offered the link flow, and active members who've
left get a fresh invite. Then it sends the message to `ask_alpha` with a note
about where the member stands (`context.py`).

`llm.py` calls Gemini (`google-genai`, model `gemini-3.5-flash`) with a system
prompt built once at startup from `resources/prompts/alpha_persona.md`
(persona and rules) and `resources/knowledge/atl_core.md` (ATL's policies and
FAQ). Errors are answered with a polite fallback, never a stack trace.
`classify_induction_intent` is a separate, much cheaper call that returns one
word, defaulting to "onboarding" on any error.

### Scheduled message cleanup (`src/common/cleanup.py`)

Features call `db.schedule_deletion(chat_id, message_id, seconds)` rather
than deleting straight away, so groups stay tidy without messages vanishing
mid-conversation. `sweep_deletions` runs every 5 minutes (first run 10s after
startup) and deletes whatever is due. A failure is logged and the row cleared
anyway, so nothing is retried forever. This needs the
`python-telegram-bot[job-queue]` extra; without it, `main.py` logs an error
and runs without the job.

## Project structure

```
atlbot/
├── main.py                     # Entry point: registers every handler in priority order, runs the bot
├── requirements.txt
├── .env.example                # Every setting, with placeholders
│
├── src/
│   ├── config.py               # Settings from .env
│   ├── commands.py             # /start deep-link router, /chatid, /kyc
│   │
│   ├── onboarding/             # New members, from the induction group to the main group
│   │   ├── induction.py        # Welcome, "I'm ready" post, admin review of the post
│   │   ├── kyc.py              # Registration questions in DM; retry rules after a decline
│   │   ├── access_review.py    # Admin approve/decline of a finished registration
│   │   └── messages.py         # Longer member-facing texts
│   │
│   ├── members/                # Existing members
│   │   ├── main_group.py       # Joins, leaves, removals, invite links, "removed" text
│   │   ├── legacy.py           # Recognising members from the old website
│   │   ├── profile.py          # View details, fill in what's missing, per-field approval card
│   │   └── profile_edit.py     # Change details on file, grouped approval card
│   │
│   ├── assistant/              # Alpha, the LLM
│   │   ├── chat.py             # DM catch-all
│   │   ├── context.py          # What Alpha is told about the member's status
│   │   └── llm.py              # Gemini client
│   │
│   ├── kyc_form/               # The KYC questions, shared by onboarding, members and the import
│   │   ├── fields.py           # The question list, labels, display formatting
│   │   └── validators.py       # Checking and cleaning answers
│   │
│   ├── common/                 # Used by every feature
│   │   ├── telegram_helpers.py # send_file, deep links, mentions, admin-card names
│   │   └── cleanup.py          # The scheduled-deletion job
│   │
│   └── db/                     # SQLite data layer; `from src import db` gives all of it
│       ├── __init__.py         # Index of every db function, by module
│       ├── schema.py           # Every table, and the member statuses
│       ├── connection.py       # get_conn(), init_db()
│       ├── members.py          # Members, status changes, audit events
│       ├── onboarding.py       # Applications, KYC answers, registration prompts
│       ├── deletions.py        # Scheduled deletions
│       ├── legacy.py           # Old-website records, matching keys, linking
│       ├── profile.py          # Profile sessions, edit sessions, approvals
│       └── main_group.py       # Owner-added members, invites, in-group nudges
│
├── resources/                  # Read by src/assistant/llm.py at startup
│   ├── prompts/alpha_persona.md
│   └── knowledge/atl_core.md
│
├── scripts/                    # Standalone tools, run by hand or on a schedule
│   ├── import_legacy.py        # One-time import of the old website's spreadsheet
│   ├── backup_db.py            # Nightly backup to S3
│   └── list_model.py           # List Gemini models for the API key
│
├── data/                       # gitignored: local exports and backups
├── secrets/                    # gitignored: local credential files
├── atl_bot.db                  # gitignored: the SQLite database
└── .env                        # gitignored: settings
```

## Getting started

**Prerequisites:** Python 3.11+, a Telegram bot token, and a Gemini API key.

```bash
git clone https://github.com/Alpha-Training-Lab/atlbot.git
cd atlbot

python3 -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate

pip install -r requirements.txt

cp .env.example .env           # then fill in real values — see below
```

Alpha needs admin rights in three groups:

- **Main group** (`MAIN_GROUP_ID`): *Invite users via link* to create invite
  links, and *Ban users* to remove people who skipped onboarding. Being an
  admin is also what lets Alpha see joins and leaves.
- **Onboarding group** (`ONBOARDING_GROUP_ID`): post review cards and read
  admins' replies.
- **Induction group** (`INDUCTION_GROUP_ID`): read every message and delete
  messages.

## Environment variables

Set these in `.env` at the project root (loaded by `src/config.py`). To find a
chat ID, add the bot to the group and send `/chatid` there.

| Variable | Required | Default | Description |
|----------|:--------:|---------|-------------|
| `BOT_TOKEN` | ✅ | — | Telegram bot token from [@BotFather](https://t.me/BotFather). |
| `GEMINI_API_KEY` | ✅ | — | API key for the Gemini model behind Alpha's replies and intent classification. |
| `ONBOARDING_GROUP_ID` | ✅ | — | Group where admins review applications, registrations and profile changes. |
| `MAIN_GROUP_ID` | Effectively required | `0` | The main ATL group. Needed for invite links, legacy linking and all main-group checks. |
| `INDUCTION_GROUP_ID` | Effectively required | `0` | The induction group: welcomes, the "I'm ready" post and the minimum-time check. |
| `OWNER_USER_ID` | Recommended | `0` | The one person who may add members to the main group directly; alerted when anyone else's join is reversed. **Unset = nobody is removed.** |
| `ONBOARDING_ENTRY_URL` | Optional | — | Where removed joiners are sent to start onboarding (e.g. the induction group link). Unset = "contact an ATL admin". |
| `INDUCTION_GROUP_PINNED_MESSAGE` | Optional | — | URL of the induction material, used in welcome and reminder messages. |
| `REQUIRED_TAGS` | Optional | `@ShemmyCypher,@Dr_evidence,@Epitome61` | Comma-separated handles an induction post must tag. |
| `MIN_INDUCTION_SECONDS` | Optional | 10 days | Minimum time in the induction group before a post is accepted. |
| `ATL_DB_PATH` | Optional | `<project root>/atl_bot.db` | Where the SQLite file lives; `~` is expanded. |
| `ATL_BACKUP_BUCKET` | For backups | — | S3 bucket used by `scripts/backup_db.py`. |

Blank values are treated as unset. These are fixed in `src/config.py` rather
than read from the environment: `MAX_DECLINES` (3), `COOLDOWN_SECONDS` (6h),
`INVITE_TTL_SECONDS` (48h), `WELCOME_DELETE_SECONDS` (24h),
`REMINDER_DELETE_SECONDS` (3h), `REGISTRATION_PROMPT_DELETE_SECONDS` (24h).

## Running the bot

```bash
python main.py
```

This creates any missing tables (`db.init_db()`), registers the cleanup job
and every handler, and starts long-polling with
`allowed_updates=Update.ALL_TYPES`, so joins, leaves and join requests are
delivered too. On the server it runs as the `atlbot` systemd service.

Only run **one** instance per `BOT_TOKEN`: Telegram rejects a second
`getUpdates` with a `Conflict` error. Don't start the bot locally with the
production token while the server is running.

## Database

SQLite is the single source of truth. All tables are defined in
`src/db/schema.py` and created at startup; every query goes through
`get_conn()`, which commits on success, rolls back on any error and always
closes, so a multi-step change (like a status change plus its audit event)
never half-applies.

| Table | Holds |
|-------|-------|
| `members` | One row per Telegram user: name fields, `status`, KYC progress. |
| `member_events` | Append-only audit log: every status change (`status:<name>`) and events like `joined_induction`, `legacy_linked`, `profile_change_approved`. |
| `applications` | Induction "I'm ready" posts and the admin's decision. |
| `kyc_responses` | One answer per member per question: text or a file reference, with `needs_review`. |
| `registration_prompts` | The induction-group "tap to register" post, so it can be deleted once used. |
| `scheduled_deletions` | Messages waiting for the cleanup job. |
| `legacy_members`, `legacy_responses` | The old website's spreadsheet, and which Telegram user claimed each row. |
| `profile_sessions` | Members part-way through filling in missing details. |
| `edit_sessions` | Members part-way through an edit; the draft stays here until they confirm. |
| `pending_changes` | Profile details waiting for admin approval, and the decision. |
| `main_group_invites` | Each member's current one-time invite link, so it's re-sent rather than re-made. |
| `group_prompts` | When someone was last nudged in the main group to update their details. |

The database holds real member PII (names, phone numbers, addresses, ID
photos by reference) and is gitignored. It must never be committed.

## Bot commands

| Command | Description |
|---------|-------------|
| `/start` | Deep-link entry point: `?start=kyc` begins or resumes registration, `?start=link` is the "I'm already an ATL member" flow. With no payload, an active member sees their profile. |
| `/profile` | Show your profile, with buttons to fill in or edit details. |
| `/kyc` | Enter registration without the deep link. Marked TEMPORARY. |
| `/chatid` | Replies with the current chat's ID, for filling in `.env`. |

## Scripts

Standalone tools in `scripts/`, run from the project root with the venv:

- `import_legacy.py <csv> [--commit]`: one-time import of the old website's
  spreadsheet into `legacy_members`. Dry run by default; the report holds
  counts only, never member data. Uses the same validators and matching keys
  as the live bot.
- `backup_db.py`: hot copy of the database, integrity check, upload to S3,
  keep the newest 14 copies locally.
- `list_model.py`: list the Gemini models available to the API key.

`inspect_db.py`, `inspect_data.py`, `wipe_test_data.py` and `test_gemini.py`
are local-only and gitignored. `inspect_data.py` prints PII; never share its
output.

## Editing the KYC form

The questions live in one place: `src/kyc_form/fields.py`. Each entry has a
`key`, a `label` (shown on profiles and admin cards), a `prompt`, a `type`
(`text`, `email`, `phone`, `day_month`, `choice` or `document`), an `order`
and an `active` flag. `"edit": "self"` lets members change it without
approval; anything else needs an admin.

- **Add a question:** add a dict with an `order`.
- **Remove a question:** set `"active": False`. Answers already collected are
  kept and no migration is needed.
- **Add an answer type:** add it to `_DISPATCH` in
  `src/kyc_form/validators.py`, plus any special handling (like the
  `document` type's uploads) in `onboarding/kyc.py` and the profile flows.

## Known limitations

- **`handle_join_request` is dormant.** The bot's invite links use
  `member_limit=1`, which never raises join requests. The handler is kept in
  `members/main_group.py` as a safety net in case the invite strategy changes.
- **Intent classification is skipped when every required tag is present.**
  A post that tags every admin but is really a question is treated as an
  onboarding submission.
- **Unlinked old-website members who rejoin the main group are removed** once
  `OWNER_USER_ID` is set, unless they come through the owner's link. Joins
  are deliberately not matched against the spreadsheet by username: anyone
  can take over a username a member has since dropped, and linking would hand
  them that member's record.
- **No automated test suite.** Verification is manual plus static checks.
