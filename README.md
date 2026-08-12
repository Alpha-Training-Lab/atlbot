# Alpha — ATL Onboarding & Community Bot

Alpha is the Telegram bot for **Alpha Training Lab (ATL)**. It walks prospective
members through induction and KYC (identity verification), routes completed
applications to admins for approval, answers member questions with a
Gemini-backed conversational assistant, and handles leadership meeting
reminders.

## Contents

- [How it works](#how-it-works)
- [Project structure](#project-structure)
- [Getting started](#getting-started)
- [Environment variables](#environment-variables)
- [Running the bot](#running-the-bot)
- [Database](#database)
- [Bot commands](#bot-commands)
- [Development scripts](#development-scripts)
- [Editing the KYC form](#editing-the-kyc-form)

## How it works

Every member has one `status` in the `members` table that drives the whole
flow:

```
pending_summary → pending_review → awaiting_dm → kyc_in_progress
      → pending_access → active
                       ↘ declined ↗ (up to 3 attempts, 6h cooldown)
```

1. **Induction** — a new member reads the induction material in the
   induction group, then posts a summary and tags the admins
   (`pending_summary` → `pending_review`).
2. **Approval out of induction** — an admin approves them, moving them to
   `awaiting_dm` and prompting them to DM the bot.
3. **KYC** (`src/modules/kyc.py`) — the bot asks each active field from
   `src/kyc_fields.py` one at a time (text, choice buttons, or document
   upload), validating answers as it goes (`src/validators.py`). Status
   becomes `kyc_in_progress`, then `pending_access` once every field is
   answered.
4. **Admin review** (`src/modules/admin.py`) — a review card with the
   member's answers and Approve/Decline buttons is posted to the onboarding
   group. Approval sets `active` and sends the welcome message; decline
   records a reason, notifies the member, and lets them restart (capped at
   `MAX_DECLINES` attempts with a cooldown between tries).
5. **Alpha, the assistant** (`src/modules/alpha.py`) — any other DM is
   answered by Gemini (`src/llm.py`), which is given the member's current
   status as context so it gives accurate, status-aware guidance instead of
   generic answers.
6. **Leadership reminders** (`src/modules/leadership.py`) — members of the
   leadership group can opt in to meeting reminders via a deep link
   (`/start leadership`).

## Project structure

```
atlbot/
├── main.py                    # Entry point — builds the bot, registers handlers
├── config.py                  # Loads and validates environment variables
├── requirements.txt
│
├── src/
│   ├── db.py                  # SQLite access layer (schema, queries, transactions)
│   ├── llm.py                 # Gemini client wrapper for Alpha
│   ├── validators.py          # KYC answer validation/normalisation
│   ├── kyc_fields.py          # Single source of truth for the KYC question list
│   ├── messages.py            # Shared message templates (e.g. welcome message)
│   └── modules/                # One file per conversational flow / handler group
│       ├── kyc.py             # Walks a member through registration
│       ├── admin.py           # Review card + approve/decline workflow
│       ├── alpha.py           # LLM-backed catch-all assistant
│       └── leadership.py      # Leadership reminder opt-in
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

## Environment variables

Set these in a `.env` file at the project root (loaded automatically via
`python-dotenv`):

| Variable               | Required | Description                                                                 |
|-------------------------|:--------:|-------------------------------------------------------------------------------|
| `BOT_TOKEN`             | ✅       | Telegram bot token from [@BotFather](https://t.me/BotFather).                |
| `GEMINI_API_KEY`        | ✅       | API key for the Gemini model that powers Alpha's conversational replies.     |
| `ONBOARDING_GROUP_ID`   | ✅       | Chat ID of the group where admin review cards and induction posts happen.    |
| `MAIN_GROUP`            | ✅       | Invite link for the main ATL group, used in the approved-member welcome text.|
| `LEADERSHIP_GROUP_ID`   | Optional | Chat ID of the leadership group, used to gate the reminder opt-in.           |
| `ATL_DB_PATH`           | Optional | Override for the SQLite file path. Defaults to `atl_bot.db` at the project root. |

To find a chat ID, temporarily run the bot and use the built-in `/chatid`
command inside the target group.

## Running the bot

```bash
python main.py
```

This initializes the SQLite schema if it doesn't exist yet, then starts
long-polling for updates.

## Database

SQLite is the single source of truth (`src/db.py`), with four tables:

- **members** — one row per Telegram user: profile fields, onboarding
  `status`, and KYC progress (`kyc_field_index`, `kyc_attempts`).
- **applications** — one row per onboarding attempt, with the admin's
  approve/decline decision and who made it.
- **member_events** — append-only audit log (status changes, restarts,
  document views, etc.).
- **kyc_responses** — one row per `(member, field)` answer, either as text
  or an uploaded file reference.

The database file (`atl_bot.db`) is created at the project root the first
time the bot runs, and is gitignored — it holds real member PII and must
never be committed.

## Bot commands

| Command   | Description                                                        |
|-----------|---------------------------------------------------------------------|
| `/start`  | Entry point; also handles the `/start leadership` deep link.        |
| `/kyc`    | Manually (re)enter the registration flow.                           |
| `/chatid` | Replies with the current chat's ID — useful for filling in `.env`.  |

## Development scripts

Everything in `scripts/` is a standalone utility, not part of the running
bot — run with `python -m scripts.<name>` (or `python scripts/<name>.py`)
from the project root, with the venv active:

- `inspect_db.py` — print the schema, row counts, and indexes of the local DB.
- `inspect_data.py` — print stored member/event rows. **Contains PII —
  local use only, never commit its output.**
- `wipe_test_data.py` — interactively delete all member data. **Local
  testing only.**
- `list_model.py` — list Gemini models available to your API key.
- `test_gemini.py` — send one manual test prompt to Gemini.

## Editing the KYC form

The questions Alpha asks during registration live in one place:
`src/kyc_fields.py`. Each entry is a dict with a `type` (`text`, `email`,
`phone`, `day_month`, `choice`, or `document`), a `prompt`, and an `active`
flag.

- **Add a question:** append a new dict and give it an `order`.
- **Remove a question:** set `"active": False` — this keeps previously
  collected answers intact rather than requiring a schema migration.
