# Alpha — ATL Onboarding & Community Bot

Alpha is the Telegram bot for **Alpha Training Lab (ATL)**. It runs the entire
member journey end to end: welcoming new arrivals in the induction group,
gating how long they must spend with the induction material, distinguishing
a genuine question from a readiness signal, routing completed applications
to admins for approval, walking approved members through identity
verification (KYC), generating single-use invite links into the main group,
keeping the induction group tidy via scheduled message deletion, and
answering ad-hoc member questions with a Gemini-backed conversational
assistant.

This document describes the system **as the code currently behaves**,
including a couple of features that exist in the codebase but are not
currently wired up (see [Known limitations](#known-limitations)).

## Contents

- [Overview](#alpha--atl-onboarding--community-bot)
- [The member lifecycle](#the-member-lifecycle)
- [How it works](#how-it-works)
  - [1. Induction](#1-induction-srcmodulesinductionpy)
  - [2. Registration (KYC)](#2-registration-kyc-srcmoduleskycpy)
  - [3. Admin access review](#3-admin-access-review-srcmodulesadminpy)
  - [4. Alpha, the assistant](#4-alpha-the-assistant-srcmodulesalphapy-srcllmpy)
  - [5. Scheduled message cleanup](#5-scheduled-message-cleanup)
  - [6. Leadership reminders](#6-leadership-reminders-not-currently-wired-up)
- [Project structure](#project-structure)
- [Getting started](#getting-started)
- [Environment variables](#environment-variables)
- [Running the bot](#running-the-bot)
- [Database](#database)
- [Bot commands](#bot-commands)
- [Scheduled jobs](#scheduled-jobs)
- [Development scripts](#development-scripts)
- [Editing the KYC form](#editing-the-kyc-form)
- [Known limitations](#known-limitations)

## The member lifecycle

Every member has exactly one `status` column in the `members` table
(`src/db.py`), and that single value drives every decision the bot makes
about what to say to them and what buttons to show. The statuses are:

```
pending_summary  →  pending_review  →  awaiting_dm  →  kyc_in_progress
                                                             │
                                                             ▼
                                                       pending_access
                                                        ╱          ╲
                                                   active          declined
                                                                   (loops back
                                                                to kyc_in_progress,
                                                              up to MAX_DECLINES=3
                                                            attempts, 6h cooldown
                                                               once capped)
```

- **`pending_summary`** — default status for every new row. The member is
  either still in the induction group reading, or was sent back here by an
  admin who said "not yet."
- **`pending_review`** — the member tagged the bot (and the required admin
  handles) with what the bot classified as an onboarding-readiness post; an
  admin review card is waiting in the onboarding group.
- **`awaiting_dm`** — an admin approved the induction post. The member has
  been told to DM the bot to begin registration, but hasn't started KYC yet.
- **`kyc_in_progress`** — the member is mid-way through the KYC field list
  (`src/kyc_fields.py`); `kyc_field_index` tracks exactly which question
  they're on.
- **`pending_access`** — every active KYC field has been answered. A review
  card with all their answers is waiting in the onboarding group.
- **`active`** — an admin approved final access. The member has received a
  single-use personal invite link to the main ATL group (or a note that an
  admin will follow up if link creation failed).
- **`declined`** — an admin declined the KYC review, with a reason. The
  member can restart immediately unless they've hit `MAX_DECLINES` (3),
  in which case there's a `COOLDOWN_SECONDS` (6 hour) wait before they can
  try again.
- **`removed`** — defined in the schema's `CHECK` constraint and in
  `ALL_STATUSES`, reserved for an admin manually removing a member for
  misconduct. **No code path currently sets this status** — there is no
  "remove member" command or handler yet.

## How it works

### 1. Induction (`src/modules/induction.py`)

This module owns everything that happens in the **induction group**
(`INDUCTION_GROUP_ID`) before a member is handed off to KYC.

**On join** (`handle_new_member`, triggered by a `NEW_CHAT_MEMBERS` status
update): the bot upserts the member row, logs a `joined_induction` event
(used later to measure how long they've been in the group), sends the
`WELCOME_INDUCTION` message (pointing them at `INDUCTION_PINNED_URL`), and
schedules that welcome message for deletion after `WELCOME_DELETE_SECONDS`
(24h) so old join spam doesn't pile up in the group.

**Catching the tag** (`handle_induction_post`, on any text message in the
group): the bot only reacts if `_bot_was_tagged` returns true — either the
message `@`-mentions the bot's own username, or it's a reply to a message
this specific bot sent. Everything else is ignored. Once tagged:

1. **Status short-circuits.** If the member is already `pending_review`,
   `awaiting_dm`, or further along (`kyc_in_progress`, `pending_access`,
   `active`, `declined`), the bot replies with a status-appropriate message
   instead of re-processing the post, and schedules that reply (paired with
   the member's own message) for deletion after `REMINDER_DELETE_SECONDS`
   (3h) via `_schedule_pair`.
2. **Tag completeness check** (`_missing_tags`). The post must mention every
   handle in `REQUIRED_TAGS` (the onboarding admins) as well as the bot
   itself. `_missing_tags` does a regex word-boundary search per tag so
   `@Dr_evidence` doesn't false-positive against `@Dr_evidence2`.
3. **Intent classification.** If (and only if) required tags are missing,
   the bot asks Gemini (`classify_induction_intent`, `src/llm.py`) whether
   the post is a genuine **question** or a signal that the member believes
   they're ready to move on (**onboarding**). This call is skipped — and
   `onboarding` is assumed — whenever all required tags are already present,
   since tagging every admin correctly is treated as a strong enough signal
   of submission intent on its own. See
   [Known limitations](#known-limitations) for the edge case this creates.
4. **Question path.** A classified question is answered publicly by
   `ask_alpha` (`src/llm.py`) with `GROUP_REPLY_CONTEXT` appended to the
   system context — this instructs Alpha to keep group replies short (≤3
   sentences) and to never post a group invite link publicly. Both the
   member's question and Alpha's answer are scheduled for deletion together
   after `REMINDER_DELETE_SECONDS`.
5. **Onboarding path — minimum time gate.** `db.seconds_since_last_event`
   checks how long it's been since `joined_induction`. If that's under
   `MIN_INDUCTION_SECONDS` (10 days by default, or overridden directly via
   the `MIN_INDUCTION_SECONDS` env var), the bot replies with a
   human-readable "come back in about N days/hours/minutes" message
   (`_humanise`) and schedules the pair for deletion. Members who joined
   before the bot was running (no `joined_induction` event on record) skip
   this gate entirely — there's no way to know when they actually joined.
6. **Onboarding path — incomplete tags.** If required tags are still
   missing at this point (classified `onboarding` despite missing tags), the
   bot replies with `INDUCTION_POST_INCOMPLETE`, logs an
   `induction_post_incomplete` event, and schedules the pair for deletion.
7. **Valid submission.** `db.create_application` records the post
   (including the exact source chat/message id, so admins can jump straight
   to it), status moves to `pending_review`, the bot acknowledges the member
   (deleted after `WELCOME_DELETE_SECONDS` — the member's own post is left
   alone, since it's needed for admin review), and `send_summary_card`
   posts a review card to the onboarding group with the member's Telegram
   handle, bot-observed time in the group, and a direct link to their post.

**The decision** (`handle_induction_decision`, callback pattern `ind:`): an
admin taps **✅ Approve** or **❌ Not yet** on the review card.
`db.decide_application` is the single source of truth for "has this already
been decided" (its `UPDATE ... WHERE decision IS NULL` is atomic, so two
admins tapping at once can't double-process one application). Either way,
the original induction post is deleted from the group (so a stale,
correctly-tagged post doesn't stay visible as a template for others to
copy). On approve, status becomes `awaiting_dm` and the member gets a
public mention with a "Start registration" deep link
(`?start=kyc`). On decline, status returns to `pending_summary` and the
member is told, publicly, to spend more time with the material.

**Join requests** (`handle_join_request`): approves or declines a Telegram
join request based on whether the member's status is `active`. **This
handler is currently dormant** — see
[Known limitations](#known-limitations).

### 2. Registration (KYC) (`src/modules/kyc.py`)

Once a member DMs the bot (via the `?start=kyc` deep link or `/kyc`),
`handle_kyc_entry` branches on their current status:

- `awaiting_dm` → starts KYC fresh (`start_kyc`): sets `kyc_in_progress`,
  resets `kyc_field_index` to 0, and sends the first field.
- `kyc_in_progress` → resumes exactly where they left off.
- `declined` → checks the decline cap/cooldown (see below), then restarts.
- `pending_access` / `active` → tells them there's nothing more to do.
- anything else (still in induction) → tells them to finish induction
  first.

Fields are asked one at a time from `active_fields()`
(`src/kyc_fields.py`), which sorts the active subset by an `order` integer.
Each answer is validated by `src/validators.py` before being saved
(`_save_and_advance`) and the index bumped. Choice-type fields use inline
keyboard buttons (`kyc:{field_index}:{option_index}` callback data);
document-type fields require a photo or file upload. A per-field
`kyc_attempts` counter (reset on every successful advance) caps retries at
`MAX_ATTEMPTS = 3`; on the third bad attempt the answer is saved anyway with
`needs_review=1` so an admin can follow up, and the flow moves on rather
than getting the member stuck.

Once every active field has an answer, `finish_kyc` sets `pending_access`
and calls `admin.send_access_request` to post the review card. A
`declined` member gets three total attempts (`MAX_DECLINES`) before a
`COOLDOWN_SECONDS` (6h) wait is enforced between further tries.

### 3. Admin access review (`src/modules/admin.py`)

`build_kyc_card` renders every active field's answer (or a "needs review"
flag) plus the member's decline history, and posts it to
`ONBOARDING_GROUP_ID` with **View documents / Approve / Decline** buttons
(`handle_access_decision`, callback pattern `acc:`).

- **View documents** DMs the reviewing admin every uploaded photo/file, one
  message per document, without changing any state.
- **Approve** sets `active`, edits the card to show who approved it, then
  calls `_personal_invite` to create a **single-use** Telegram invite link
  into `MAIN_GROUP_ID` (`member_limit=1`, expiring after
  `INVITE_TTL_SECONDS` — 48h) and DMs it to the member via
  `welcome_approved()`. If invite creation fails for any reason (the bot
  isn't an admin in the main group, a bad `MAIN_GROUP_ID`, a Telegram API
  error), the exception is caught and logged, and the member instead gets a
  message saying an admin will follow up — approval never fails just
  because the invite link couldn't be created.
- **Decline** opens a checkbox-style reason picker (`_reason_keyboard`,
  toggled via `acc:tog:`) built from `DECLINE_REASONS`, with an "Other" flow
  that posts a `ForceReply` prompt in the onboarding group so an
  admin can type a custom reason (caught by `handle_decline_reason`).
  Confirming (`acc:cfm:`) calls `_finalise_decline`, which sets `declined`,
  tells the member (with a "start again" button unless they've hit
  `MAX_DECLINES`), and updates the card.

### 4. Alpha, the assistant (`src/modules/alpha.py`, `src/llm.py`)

Any private DM that isn't mid-KYC and isn't a command falls through to
`handle_alpha_message` (registered in a *lower-priority handler group* than
everything else in `main.py`, so it only ever sees messages nothing else
claimed). It sends a "typing…" action, looks up the member's status,
maps that status to a human-readable context string via `STATUS_CONTEXT`
(or `NO_RECORD` if the person has no row at all), and passes the member's
message plus that context to `ask_alpha`.

`ask_alpha` (`src/llm.py`) calls the Gemini API (`google-genai`, model
`gemini-3.5-flash`) with a `SYSTEM_INSTRUCTION` built once at import time
from `resources/prompts/alpha_persona.md` (Alpha's persona and rules,
including "never post a group invite link" and "never notify/escalate to
admins yourself") plus `resources/knowledge/atl_core.md` (ATL's actual
policies, membership rules, and FAQ-style knowledge). Any per-message
`context_note` (member status, or `GROUP_REPLY_CONTEXT` for induction-group
replies) is appended on top. API errors and unexpected exceptions are both
caught and answered with a generic `FALLBACK` message rather than crashing
the handler or leaking a stack trace to the member.

The same module exposes `classify_induction_intent`, used only by
`induction.py` (see above) — a separate, much cheaper Gemini call
(`max_output_tokens=20`) constrained to reply with exactly one word,
defaulting to `"onboarding"` on any error or ambiguous response.

### 5. Scheduled message cleanup

Several flows above call `db.schedule_deletion(chat_id, message_id,
seconds)` instead of deleting a message immediately, so groups stay
readable without messages vanishing mid-conversation. Rows live in the
`scheduled_deletions` table. A periodic job (`induction.sweep_deletions`,
registered on `app.job_queue` in `main.py` with `interval=300` seconds and
`first=10`) polls `db.due_deletions()` every 5 minutes, deletes each
message (a failure — already deleted, bot lacks permission, etc. — is
logged and the row is cleared anyway so it's never retried forever), and
clears the row via `db.clear_deletion`. This requires the
`python-telegram-bot[job-queue]` extra (APScheduler) to be installed — see
[Environment variables](#environment-variables) / `requirements.txt`; if
it's missing, `main.py` logs an error and simply skips registering the job
rather than crashing.

### 6. Leadership reminders (not currently wired up)

`src/modules/leadership.py` exists (membership check against
`LEADERSHIP_GROUP_ID`, a `handle_registration` handler meant to be reached
via a `/start leadership` deep link) but **is not connected to anything** —
`main.py` imports the module but never registers a handler for it, and the
`/start` command only recognizes the `kyc` payload. See
[Known limitations](#known-limitations).

## Project structure

```
atlbot/
├── main.py                    # Entry point — builds the bot, registers handlers, runs the job queue
├── config.py                  # Loads and validates environment variables
├── requirements.txt
│
├── src/
│   ├── db.py                  # SQLite access layer (schema, queries, transactions)
│   ├── llm.py                 # Gemini client wrapper: ask_alpha + classify_induction_intent
│   ├── validators.py          # KYC answer validation/normalisation
│   ├── kyc_fields.py          # Single source of truth for the KYC question list
│   ├── messages.py            # Shared message templates (welcome, induction, incomplete-post)
│   └── modules/                # One file per conversational flow / handler group
│       ├── induction.py       # Induction-group welcome, tag-gate, admin review, cleanup sweep
│       ├── kyc.py             # Walks a member through registration
│       ├── admin.py           # KYC review card + approve/decline + personal invite links
│       ├── alpha.py           # LLM-backed catch-all assistant
│       └── leadership.py      # Leadership reminder opt-in — NOT currently wired into main.py
│
├── resources/                  # Content read by src/llm.py at import time
│   ├── prompts/alpha_persona.md   # Alpha's system prompt / persona
│   └── knowledge/atl_core.md      # ATL knowledge base given to the LLM
│
├── scripts/                    # Standalone dev/maintenance utilities (not imported by the app)
│   ├── inspect_db.py          # Dump schema + row counts for the local DB
│   ├── inspect_data.py        # Print stored member/event rows (contains PII — local use only)
│   ├── wipe_test_data.py      # Delete all member data (local testing only)
│   ├── list_model.py          # List available Gemini models for the configured API key
│   └── test_gemini.py         # Manual smoke test against the Gemini API
│
├── data/                        # gitignored — local exports (e.g. member CSV dumps)
├── secrets/                     # gitignored — local credential files (e.g. atlbot.pem)
├── atl_bot.db                   # gitignored — local SQLite database file
└── .env                         # gitignored — local environment variables
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

> There's no `.env.example` in the repo yet — see [Environment variables](#environment-variables)
> for the full list of keys to put in your own `.env` file.

For the bot to actually approve members into the main group, it must be an
**admin** in `MAIN_GROUP_ID` with permission to invite users (so it can
call `create_chat_invite_link`), and an admin in `ONBOARDING_GROUP_ID` and
`INDUCTION_GROUP_ID` (so it can post/delete messages and read all updates
there).

## Environment variables

Set these in a `.env` file at the project root (loaded automatically via
`python-dotenv`). To find a chat ID, temporarily run the bot and use the
built-in `/chatid` command inside the target group.

| Variable                        | Required | Default                     | Description |
|----------------------------------|:--------:|------------------------------|--------------|
| `BOT_TOKEN`                      | ✅       | —                             | Telegram bot token from [@BotFather](https://t.me/BotFather). |
| `GEMINI_API_KEY`                 | ✅       | —                             | API key for the Gemini model that powers Alpha's replies and intent classification. |
| `ONBOARDING_GROUP_ID`            | ✅       | —                             | Chat ID of the group where KYC/induction review cards are posted and decided. |
| `MAIN_GROUP_ID`                  | Effectively required | `0`             | Chat ID of the main ATL group. Used to generate each approved member's single-use invite link — without it, `create_chat_invite_link` will fail (caught; the member is told an admin will follow up instead). |
| `INDUCTION_GROUP_ID`             | Effectively required | `0`             | Chat ID of the induction group — welcome messages, tag-gating, and the minimum-time check all key off this. |
| `INDUCTION_GROUP_PINNED_MESSAGE` | Optional | `None`                        | URL pointed to as "the induction material" in welcome/reminder messages (read as `INDUCTION_PINNED_URL`). |
| `LEADERSHIP_GROUP_ID`            | Optional | `0`                           | Chat ID of the leadership group. Currently unused in practice — see [Known limitations](#known-limitations). |
| `ATL_DB_PATH`                    | Optional | `<project root>/atl_bot.db`   | Override for the SQLite file path. Accepts a relative or absolute path; `~` is expanded. |
| `MIN_INDUCTION_SECONDS`          | Optional | `MIN_INDUCTION_DAYS * 86400`  | Minimum time (in **seconds**) a member must be in the induction group before their post is accepted. Overrides `MIN_INDUCTION_DAYS` (hardcoded to `10`) if set. |
| `REQUIRED_TAGS`                  | Optional | `@ShemmyCypher,@Dr_evidence,@Epitome61` | Comma-separated list of handles a member's induction post must tag before it's forwarded for review. |

**Not currently read by the code, but present in a typical `.env` for this
project:** a long list of ATL group/class link variables (e.g.
`ATL_MARKET_PLACE`, `ATL_FUN_GROUP`, `INTRODUCTORY_CLASSES_*`,
`MICROSOFT_EXCEL_FEB2022`, etc.). These aren't referenced anywhere in
`config.py` or elsewhere in the codebase today — they appear to be reserved
for features not yet built. Don't rely on them being wired up.

**Removed:** earlier versions of this bot read a static `MAIN_GROUP` invite
link and sent it to every approved member. That has been replaced entirely
by the per-member, single-use invite links described above — `config.py` no
longer reads `MAIN_GROUP` at all, and `src/messages.py` no longer contains
a message that embeds a static group link, by design (the static link
should never be posted or embedded anywhere the bot can leak it).

The following are hardcoded in `config.py` rather than read from the
environment: `MAX_DECLINES` (3), `COOLDOWN_SECONDS` (6h), `INVITE_TTL_SECONDS`
(48h), `WELCOME_DELETE_SECONDS` (24h), `REMINDER_DELETE_SECONDS` (3h).
Change them directly in `config.py` if you need different values.

## Running the bot

```bash
python main.py
# or: python -m main
```

This initializes the SQLite schema if it doesn't exist yet (`db.init_db()`),
registers the `sweep_deletions` job on the job queue (if the `job-queue`
extra is installed), registers every handler described above, and starts
long-polling for updates (`allowed_updates=Update.ALL_TYPES`, so chat-join
requests and other non-message updates are delivered too).

Only run **one** instance of the bot against a given `BOT_TOKEN` at a time —
Telegram's long-polling API rejects a second concurrent `getUpdates` call
with a `Conflict` error.

## Database

SQLite is the single source of truth (`src/db.py`), with five tables. Every
write goes through `get_conn()`, a context manager that commits on success,
rolls back and re-raises on any exception, and always closes the
connection — so a partial failure inside a multi-statement function (like
`set_status`, which updates `members` and inserts into `member_events` in
one call) never leaves the database half-updated.

- **`members`** — one row per Telegram user: profile fields (`username`,
  `first_name`, `last_name`), onboarding `status` (see
  [The member lifecycle](#the-member-lifecycle)), and KYC progress
  (`kyc_field_index`, `kyc_attempts`). `upsert_member` creates the row on
  first contact and refreshes profile fields on every subsequent contact
  (via `COALESCE`, so a `None` argument never overwrites a known value) —
  it never touches `status`.
- **`applications`** — one row per KYC submission, recording the admin's
  `decision` (`approved`/`declined`, `NULL` while pending), who decided it,
  and when.
- **`member_events`** — an append-only audit log: every status change (as
  `status:<name>`), plus one-off events like `joined_induction`,
  `documents_viewed`, `induction_post_incomplete`, `kyc_restarted`,
  `joined_main_group`, and `join_request_declined`. `seconds_since_last_event`
  and `count_events` both query this table directly in SQL rather than
  loading rows into Python, so time-based gates (like the induction minimum
  wait) can't drift due to clock differences between the bot process and
  the database.
- **`kyc_responses`** — one row per `(member, field)` answer, either as
  text or an uploaded file reference (`file_ref`), with a `needs_review`
  flag for anything that hit the max retry count without validating.
- **`scheduled_deletions`** — one row per message queued for deletion by
  `db.schedule_deletion`, cleared by the periodic `sweep_deletions` job (see
  [Scheduled jobs](#scheduled-jobs)).

The database file is created at the location described in
[Environment variables](#environment-variables) the first time the bot
runs, and is gitignored — it holds real member PII (names, phone numbers,
addresses, ID photos-by-reference) and must never be committed.

## Bot commands

| Command   | Description                                                        |
|-----------|---------------------------------------------------------------------|
| `/start`  | Entry point. Recognizes the `?start=kyc` deep link used throughout the induction/approval flow; anything else gets a generic pointer back to the group that invited them. |
| `/kyc`    | Manually (re)enter the registration flow — marked `TEMPORARY` in `main.py`, intended as a debug/testing convenience rather than the primary entry point (which is the `?start=kyc` deep-link button). |
| `/chatid` | Replies with the current chat's ID — useful for filling in the group-ID variables in `.env`. |

## Scheduled jobs

| Job                          | Interval | First run | Purpose |
|-------------------------------|----------|-----------|---------|
| `induction.sweep_deletions`   | 300s (5 min) | 10s after startup | Deletes every message whose scheduled time has passed and clears its `scheduled_deletions` row. |

Registered via `app.job_queue.run_repeating(...)` in `main.py::main()`.
Requires the `job-queue` extra (see `requirements.txt`); if unavailable,
`main.py` logs an error and continues without it rather than crashing.

## Development scripts

Everything in `scripts/` is a standalone utility, not part of the running
bot — run with `python -m scripts.<name>` (or `python scripts/<name>.py`)
from the project root, with the venv active:

- `inspect_db.py` — print the schema, row counts, and indexes of the local DB.
- `inspect_data.py` — print stored member/event rows. **Contains PII —
  local use only, never commit its output.**
- `wipe_test_data.py` — interactively delete all member data. **Local
  testing only.**
- `list_model.py` — list Gemini models available to your API key (useful
  for confirming a `MODEL` value in `src/llm.py` is actually available
  before relying on it).
- `test_gemini.py` — send one manual test prompt to Gemini.

## Editing the KYC form

The questions Alpha asks during registration live in one place:
`src/kyc_fields.py`. Each entry is a dict with a `type` (`text`, `email`,
`phone`, `day_month`, or `choice`, or `document`), a `prompt`, an `order`
(used to sort the active list), and an `active` flag. There are currently
15 fields defined, covering identity (full name, email, phone, birthday,
gender, country/state/address), how the member found ATL, their vouch
(referrer name + Telegram handle), and two document uploads (an ID photo
and a photo of the member holding that same ID). One field
(`newsletter_opt_in`) is defined but set `"active": False`.

- **Add a question:** append a new dict and give it an `order`.
- **Remove a question:** set `"active": False` — this keeps previously
  collected answers intact rather than requiring a schema migration.
- **Add a new answer type:** add a branch to `src/validators.py`'s
  `_DISPATCH` table, and (if it needs special handling, like the
  `document` type's file-upload logic) a corresponding branch in
  `src/modules/kyc.py::handle_kyc_message`.

## Known limitations

- **Leadership reminders are dead code in practice.** `src/modules/leadership.py`
  is imported by `main.py` but no handler is ever registered for it, and the
  `/start` command's payload check only recognizes `"kyc"` — the
  `/start leadership` deep link this module expects does not currently
  work. Even if that wiring were restored, `handle_registration` itself
  calls `db.upsert_member(user.id, user.full_name, user.username)` (wrong
  argument order for the current `upsert_member(user_id, username=None,
  first_name=None, last_name=None)` signature) and `db.register_module(...)`
  (no such function exists in `src/db.py` — it was removed in an earlier
  schema refactor). Restoring this feature needs both the `main.py` wiring
  and these two calls fixed.
- **`handle_join_request` is currently dormant.** It's registered in
  `main.py` via `ChatJoinRequestHandler`, but the only invite links the bot
  generates (`admin._personal_invite`) use `member_limit=1`, not
  `creates_join_request=True` — so joining via one of those links never
  raises a `chat_join_request` update in the first place. The handler is
  kept in place as a safety net in case the invite strategy changes back to
  join-request-based links.
- **Intent classification is skipped when required tags are present.** In
  `handle_induction_post`, `classify_induction_intent` only runs when
  `_missing_tags` finds something missing. A post that happens to tag every
  required admin *and* is actually a genuine question (rather than an
  onboarding-readiness signal) will not be classified — it's treated as an
  onboarding submission and forwarded to admin review.
- **`removed` status is defined but unreachable.** It's a valid value in
  the `members.status` `CHECK` constraint and in `db.ALL_STATUSES`, but no
  command or handler currently sets it — there's no "remove this member"
  admin action yet.
- **No automated test suite.** Verification today is manual (see
  `scripts/`) plus static checks (`python -m py_compile`, `pyflakes`) run
  ad hoc; there is no `pytest` (or similar) suite in the repo.
