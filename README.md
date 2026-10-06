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
- **The weekly brief:** every Monday, an autonomous community newsletter in
  the induction group, built from the week's activity and checked for
  anything that looks like financial advice before it is posted.
- **Owner tools:** a hand-picked list of Legacy Members, and admin roles
  (granted by the owner, or automatically for the leadership group).

Every push to `main` is linted and tested, then deployed to the production
server if the tests pass (see [Tests, CI and deployment](#tests-ci-and-deployment)).

This document describes the system **as the code currently behaves**.

## Contents

- [Architecture](#architecture)
- [The member lifecycle](#the-member-lifecycle)
- [How it works](#how-it-works)
  - [Onboarding](#onboarding-srconboarding)
  - [Existing members](#existing-members-srcmembers)
  - [Alpha, the assistant](#alpha-the-assistant-srcassistant)
  - [Legacy Members](#legacy-members-the-owners-special-list-membersspecialpy)
  - [Roles](#roles-membersrolespy)
  - [What admins see](#what-admins-see)
  - [Weekly brief](#weekly-brief-srcbrief)
  - [Repeating jobs](#repeating-jobs)
  - [Scheduled message cleanup](#scheduled-message-cleanup-srccommoncleanuppy)
- [Project structure](#project-structure)
- [Getting started](#getting-started)
- [Environment variables](#environment-variables)
- [Running the bot](#running-the-bot)
- [Tests, CI and deployment](#tests-ci-and-deployment)
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
        ┌──────────────────┼────────────────────┬──────────────────┐
        ▼                  ▼                    ▼                  ▼
  src/onboarding/     src/members/        src/assistant/      src/brief/
  new members         existing members    Alpha, the LLM      weekly brief
        │                  │                    │                  │
        └──────┬───────────┴────────────┬───────┴──────────────────┘
               ▼                        ▼
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
- `brief/` uses `members/roles.py` to decide whether a group Alpha was added
  to was added by an admin, and `assistant/llm.py` for its Gemini calls.
  `commands.py` uses `brief/mentions.py` for the `?start=mentions` deep link.

`members/` never imports `onboarding/`, `assistant/` or `brief/`.

**Who gets a message first.** `main.py` registers every handler. Telegram
updates pass through handler groups in order: −3, −2, −1, 0, then 1. Within a
group, only the first matching handler runs, and a handler can stop an update
from reaching later groups. So the order in `main.py` is the bot's priority
list:

| Group | Handler | Claims |
|------:|---------|--------|
| −3 | `brief/capture.py` | Felicitation-group posts (birthdays only), text posted in the groups the brief reads, and Alpha being added to or leaving a group. Never stops an update, so everything below still sees it |
| −2 | `members/vouch.py` | A DM from someone's named vouch: their "Hi" is how Alpha learns who they are, so it asks them first (stopped there only when it asks) |
| −1 | `members/special.py`, `members/roles.py` | The owner's user-picker answers (Legacy Members, admins), so nothing else treats them as a message |
| −1 | `members/legacy.py` | Shared phone numbers and Skip taps from the link flow (stopped there, so they never reach KYC or the LLM); silent username matching on main-group posts and DMs |
| 0 | `src/commands.py`, `members/profile.py` | `/start`, `/profile`, `/kyc`, `/chatid` |
| 0 | `members/special.py`, `members/roles.py`, `brief/mentions.py` | Owner-only commands in a DM: `/legacy`, `/legacylist`, `/legacyremove`, `/admin`, `/admins`, `/unadmin`, `/askmentions` |
| 0 | `brief/mentions.py` | `/mentions` and its buttons (`bm:`) |
| 0 | `members/roles.py` | Posts, joins and leaves in the leadership group |
| 0 | `onboarding/induction.py` | Everything in the induction group |
| 0 | `members/profile.py`, `members/profile_edit.py` | DMs from a member part-way through filling in or editing their profile |
| 0 | `onboarding/kyc.py` | DMs from a member part-way through registration |
| 0 | `onboarding/access_review.py` | Admin buttons and typed decline reasons in the onboarding group |
| 0 | `members/main_group.py` | Joins, leaves and removals in the main group |
| 0 | `members/vouch.py` | A vouch's Yes / No buttons (`vc:`) |
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
Decline** buttons (`acc:`). Once the card is up, the named vouch is asked to
confirm (see *Vouch consent* below) and the card shows their answer.

- The consent status sits on the card's vouch username line.
- **Approve** is locked until the vouch says Yes; tapping it earlier just
  explains why. Cards from before vouch consent existed, or whose vouch
  username was unusable (flagged ⚠️ NEEDS REVIEW), have no request and can be
  approved as before.
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

**Fill in missing details** asks the gaps one at a time. Grouped fields
(`FIELD_GROUPS` in `src/kyc_form/fields.py`) are always asked together: ID
type with the ID document, and vouch name with vouch username. The photo
holding the ID is separate, since members often send one photo without the
other. Fields marked `"edit": "self"` save straight away (birthday is
self-edit: it's for celebrations and isn't verified). Identity details go to
a card in the onboarding group: full name and the photo holding the ID are
approved or rejected one by one, and the ID document (type plus photo) as one
item once both have arrived. Whatever the admin should compare against is
posted under the card from what's on file (`COMPARE_WITH`): the ID for a
name, and each ID photo for the other, so the admin can check the face photo
is holding the same ID. Vouch details appear on the same card for the onboarding team, marked
"awaiting consent", with no buttons: the vouch decides them, and they never
hold up the rest. The member is told the admins' outcome as soon as the admin
items are decided, and the vouch's outcome separately.

While a session is open, every DM the member sends is taken as an answer.
So a session with no answer for `PROFILE_SESSION_IDLE_SECONDS` (60 minutes)
is closed, exactly as if they'd tapped ⏸ Stop: held vouch details with no
vouch asked yet are dropped, the card updates, and the member is told to send
`/profile` to carry on.

#### 7. Editing details on file (`profile_edit.py`)

**Edit my details** opens a menu. Vouch name and username are edited
together, as are ID type and the ID document; the photo holding the ID is
its own item.
Every edit shows current → new and waits for the member to confirm.
Self-edit fields save on confirm. Approval fields (🔒) go to the onboarding
group as one card, approved or rejected as a whole (a rejection carries a
preset reason the member sees). What's on file to compare against is posted
under the card: both ID photos for a name change, the photo holding the ID
for a new ID document, and the ID document for a new photo holding the ID. A vouch change (🤝) is asked of
the new vouch; the onboarding group gets a card showing it (and what's on
file now) that updates itself when the vouch answers.

An edit with no answer for 60 minutes (`PROFILE_SESSION_IDLE_SECONDS`) is
cancelled and its draft dropped; nothing was saved, and the member is told.

#### 8. Vouch consent (`vouch.py`)

A member names their vouch by Telegram username, at registration or later in
their profile. The vouch must confirm, and must be a full ATL member (in the
main group).

Alpha can't look up someone's id from a username, and can't message anyone
who hasn't started it. So it messages the vouch directly only when it already
knows them and they're in the main group; otherwise it posts
"@vouch, please send me Hi in your DM" in the main group (deleted after
`VOUCH_TAG_DELETE_SECONDS`). Any DM from that username, a "Hi" or the tag's
button, gets the question with ✅ Yes / ❌ No. A tap only counts from the
account that was asked, still holding that username, still in the main group.

A silent vouch is reminded every `VOUCH_REMIND_SECONDS` (12h); after
`VOUCH_EXPIRE_SECONDS` (72h) silence counts as No.

- **Registration:** Yes unlocks Approve. No, silence, or a vouch outside the
  main group declines the registration automatically through the normal
  decline path, so the member is told why and can register again naming a
  different vouch (this counts as one of their attempts). An admin decline
  stops the vouch being chased.
- **Profile:** Yes saves the vouch details; anything else discards them and
  the member is told. The onboarding card shows the vouch as awaiting
  consent, then the outcome, but it never holds up the member's other details.

At entry the username must be a valid Telegram username, not the member's
own, and belong to an **active member in the members table**
(`db.username_on_record`). The members table is the only source of truth for
validation: the old website's spreadsheet (`legacy_members`) is a backlog to
be deleted and plays no part. Telegram can't tell a bot whether a person's
username exists, so this check is what stops a typo being tagged in the main
group. Each miss asks the member to double-check; after 3, registration
passes the vouch to an admin (flagged on the card, no vouch asked, Approve
available) and the profile flows tell the member to contact an admin.

Because lookups rely on stored usernames, `legacy.passive_link` refreshes a
known member's Telegram details whenever Alpha sees them (a DM or a
main-group post, once per restart), clearing a username they've dropped and
taking it off anyone else's row. A genuine member who isn't active in the
members table yet can't be named as a vouch until they are; the third-miss
referral covers that case.

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

### Legacy Members: the owner's special list (`members/special.py`)

Special individuals the owner adds by hand are active in ATL without going
through induction, KYC or a vouch, and are tagged ⭐ Legacy Member on their
profile and on profile cards. Owner only, in a private chat with Alpha:

- `/legacy` shows a **Pick Legacy Members** button (Telegram's own user
  picker, up to 10 at once). Telegram gives Alpha their ids directly, so
  they're created, linked and active at once.
- `/legacy @name @name2` adds by username. A bot can't turn a username into
  an id, so each one waits until Alpha can trust the person holding that
  username is the right one: they're **in the main group or the leadership
  group** (they post there, join it, or message Alpha while Telegram confirms
  they're in it), or they're **already an active member on record** and
  message Alpha. Then they're linked and made active. A DM alone isn't
  enough: anyone can take a username someone else has dropped, and linking
  makes them an active member. An onboarding record isn't enough either,
  since joining the induction group creates one. A waiting Legacy Member who
  joins the main group is never removed by the gatekeeper. The picker has
  none of this uncertainty, so prefer it.
- `/legacylist` shows who's on the list and who's still waiting. Only the
  owner can see the list: the tag shows on the member's own profile, never on
  admin cards.
- `/legacyremove @name` takes someone off the list; their membership stays.

Adding someone makes them active whatever their previous status: it's the
owner's explicit choice. Picking from chats is the safer route, since a typed
username is matched to whoever holds it when they're first seen. In the code
this list is `special_members`, to keep it apart from `legacy_members`, the
old website's backlog.

### Roles (`members/roles.py`)

Every member is an ordinary member unless they hold a role in
`member_roles`; today the one role is **admin**, for resources ordinary
members won't be able to reach. The owner (`OWNER_USER_ID`) is above roles:
always passes, can't be demoted, and is the only person who can grant or
remove one. Owner only, in a private chat with Alpha:

- `/admin` shows a **Pick admins** button (Telegram's user picker), or
  `/admin @name` for people already on record.
- `/admins` lists them and where each role came from; `/unadmin @name` makes
  someone an ordinary member again (their membership is unchanged).

**The leadership group makes admins automatically.** With
`LEADERSHIP_GROUP_ID` set and Alpha an admin in that group, anyone who joins
it becomes an admin, and stops being one when they leave or are removed.
Telegram can't list a group's members, so people already in the group are
recognised the first time they post there (Alpha reads only who posted). A
role the owner grants by hand outranks this and stays even if they leave the
group; `/unadmin` won't remove a leadership-based role, since they'd get it
back on their next post: remove them from the group instead.

Granting a role makes the person active if they weren't. To gate a future
feature, use the `ADMINS` filter on its handler, or `roles.is_admin()` inside
a callback. Adding a new role means adding it to `ROLES` in `db/roles.py`; the
table needs no change. Card buttons in the onboarding group are not
role-gated yet: anyone in that group can still use them.

### Looking members up and expelling them (`members/manage.py`)

The owner, and the onboarding leads the owner names with `/obtlead @name`,
can in a private chat with Alpha:

- `/member @username` (or `/member <name>` to search) to see a member's full
  record with their ID photos. The record deletes itself after 10 minutes,
  and every lookup is logged with who looked (`member_viewed`).
- `/expel @username`, or the **Expel** button on a record: pick a reason,
  confirm, and Alpha marks them removed, bans them from every ATL group it
  manages (main, induction, leadership), tells them why, and strips any
  role, onboarding-lead access or Legacy Member place, so nothing can make
  them active again. Onboarding leads can only expel ordinary members; the
  owner is told whenever a lead expels someone.
- The owner can undo it with `/reinstate @username`. Someone who was an
  active member goes back to active; someone expelled mid-onboarding starts
  onboarding again from the induction group, so a reinstatement never skips
  induction review, KYC or the vouch. The status before removal is kept in
  `members.status_before_removal`.
- `/reinstate` is the only way back. `/admin` and `/legacy` refuse anyone
  removed (expelled, or banned from the main group) and say to use
  `/reinstate` first. The one exception: if the owner adds them to the main
  group by hand, they're active, as with anyone the owner adds directly.

Onboarding leads are a separate permission (`member_managers`), not a role:
the leadership group makes people admins automatically, and that must never
give access to members' data. `/obtlead` lists them, `/unobtlead @name`
removes one. Only active members can be made leads, and a lead who stops
being active, however that happens, loses access until they're active again.

### Exporting everything (`scripts/export_members.py`)

Owner only, on the server: `.venv/bin/python scripts/export_members.py`
writes `data/exports/atl_members_<time>.zip` with `members.xlsx` (one row per
member, every detail, links to each photo) and an `ids/` folder of the ID
photos downloaded from Telegram. `--status active` limits it to one status;
`--no-photos` skips the photos. The zip holds everyone's personal data and
IDs: copy it off, keep it private, and delete it from the server and your
computer when done. It's a generated export; the database stays the only
source of truth.

### What admins see

Cards, photo captions, prompts and invite-link names identify a member by
their @username, or their name if they have none (`handle_or_name` in
`src/common/telegram_helpers.py`). Telegram user ids never appear in
admin-facing text; the bot links cards, photos and prompts to members
internally (for example, an "Other" decline prompt is matched through the
`reason_prompts` table, not its wording). The one exception is the owner's
private alert about an unauthorised join.

### Weekly brief (`src/brief/`)

Every Monday from 09:00 UTC Alpha posts a community brief in the induction
group. It is written for people still in induction: a peek at life inside
ATL, ending with how to join the main group. It is fully autonomous: nobody
writes highlights, approves it or gets a copy.

1. **Capture** (`capture.py`, handler group -3, before everything else and
   never stopping an update): text messages from the groups the brief reads
   go into `brief_messages`. Not commands, edits, bots or anonymous admins.
   A reply counts toward "most helpful" unless it's to a bot or to themselves.
2. **Digest** (`digest.py`, hourly job, but only days that have ended): each
   group's day becomes counts plus a short Gemini digest in `brief_days`, and
   that day's messages are deleted in the same transaction. Gemini sees text
   only, never who wrote it. If Gemini fails the day is retried; after
   `BRIEF_DIGEST_GIVE_UP_DAYS` it is saved without a digest and the messages
   are deleted anyway.
3. **Brief** (`weekly.py`, every 10 minutes, acts only on Monday between
   `BRIEF_HOUR_UTC` and `BRIEF_LAST_HOUR_UTC`). From SQLite: week in numbers
   (this week's activity only, never totals; a count below `BRIEF_HIDE_BELOW`
   is left out), birthdays, milestones, most helpful. From Gemini: a summary
   of the week's discussions, the lesson of the week, members' wins and
   "Coming up this week" (events announced in the chat, with dates Gemini
   works out from the posting day; code keeps only dates in the week ahead
   that were actually announced), used
   only if they pass a pattern check in code and then a Gemini compliance
   check; otherwise those sections are left out. The safety tip rotates
   through the list in `content.py`, shuffled once and taken one per week,
   so nothing repeats until all are used.
   `brief_weeks` makes sure the brief is sent once, and the week's data is
   deleted once it is.

**Which groups:** the main group from the start; any group Alpha is added to
later if the owner or an admin added it. Never the leadership, onboarding or
induction group (`config.BRIEF_NEVER_READ`). The felicitation group
(`FELICITATION_GROUP_ID`) is read for birthdays only: a post saying happy
birthday, HBD or many happy returns records who it @mentions, and no text is
stored.

**Who is named:** only active members who answered "Yes, mention me" to the
"Weekly brief" question. New members answer it at the end of registration;
anyone can answer or change it with `/mentions` (or under `/profile`), and
the owner's `/askmentions` posts an invite with a button in the main group
(`brief/mentions.py`). No answer counts as No. Everyone celebrated is
counted; only they are named. Gemini's part never names anyone.

**Retention:** raw messages until their day is digested (normally just after
midnight UTC), digests until the brief is sent, nothing past
`BRIEF_KEEP_DAYS`. Database backups taken in between will contain them.

### Repeating jobs

`members/vouch.sweep` runs every 30 minutes: reminders and expiry for
vouch requests (above).

`brief/digest.digest_finished_days` runs every hour and
`brief/weekly.publish_weekly_brief` every 10 minutes (Weekly brief, above).

`members/profile.sweep_idle_sessions` and
`members/profile_edit.sweep_idle_sessions` run every 5 minutes and close
fill and edit sessions that have gone quiet (above). Fill sessions are closed
10 at a time, since each may edit a card in the onboarding group.

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
├── requirements.txt            # What the bot needs to run
├── requirements-dev.txt        # requirements.txt plus ruff and pytest, for tests and CI
├── .env.example                # Every setting, with placeholders
│
├── .github/workflows/ci.yml    # CI/CD: lint, compile, test; deploy to EC2 on pushes to main
├── tests/                      # pytest suite, run by CI on every PR and push to main
│   └── test_validators.py      # Every KYC answer validator, valid and invalid
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
│   │   ├── profile_edit.py     # Change details on file, grouped approval card
│   │   ├── roles.py            # Member roles (admin); the ADMINS filter
│   │   ├── special.py          # The owner's Legacy Members list
│   │   └── vouch.py            # The named vouch confirms; reminders and expiry
│   │
│   ├── assistant/              # Alpha, the LLM
│   │   ├── chat.py             # DM catch-all
│   │   ├── context.py          # What Alpha is told about the member's status
│   │   └── llm.py              # Gemini client (chat, induction intent, weekly brief)
│   │
│   ├── brief/                  # The weekly community brief
│   │   ├── capture.py          # Store group messages; track groups Alpha joins
│   │   ├── digest.py           # Daily counts + Gemini digest, then delete messages
│   │   ├── weekly.py           # Monday: build, check and post the brief
│   │   ├── mentions.py         # /mentions: may the brief name you?
│   │   └── content.py          # Safety tips (edit freely)
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
│       ├── main_group.py       # Owner-added members, invites, in-group nudges
│       ├── vouch.py            # Vouch requests, reminders, outcomes
│       ├── special.py          # The owner's Legacy Members list
│       ├── roles.py            # Member roles and where each came from
│       └── brief.py            # Weekly brief: groups, messages, digests, weeks
│
├── resources/                  # Read by src/assistant/llm.py at startup
│   ├── prompts/alpha_persona.md
│   └── knowledge/atl_core.md
│
├── scripts/                    # Standalone tools, run by hand or on a schedule
│   ├── import_legacy.py        # One-time import of the old website's spreadsheet
│   ├── backup_db.py            # Nightly backup to S3
│   ├── stats.py                # Read-only snapshot, counts only
│   ├── first_completers.py     # Read-only: who finished updating their records first
│   ├── preview_brief.py        # Build this week's brief on a copy of the db, send it to the owner
│   └── list_model.py           # List Gemini models for the API key
│
├── data/                       # gitignored: local exports and backups
├── secrets/                    # gitignored: local credential files
├── atl_bot.db                  # gitignored: the SQLite database
└── .env                        # gitignored: settings
```

## Getting started

**Prerequisites:** Python 3.14 (the server and CI run 3.14.4), a Telegram
bot token, and a Gemini API key.

```bash
git clone https://github.com/Alpha-Training-Lab/atlbot.git
cd atlbot

python3 -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate

pip install -r requirements.txt       # or requirements-dev.txt to run the tests

cp .env.example .env           # then fill in real values — see below
```

Alpha needs admin rights in these groups:

- **Main group** (`MAIN_GROUP_ID`): *Invite users via link* to create invite
  links, and *Ban users* to remove people who skipped onboarding. Being an
  admin is also what lets Alpha see joins and leaves.
- **Onboarding group** (`ONBOARDING_GROUP_ID`): post review cards and read
  admins' replies.
- **Induction group** (`INDUCTION_GROUP_ID`): read every message and delete
  messages. The weekly brief is posted here.
- **Leadership group** (`LEADERSHIP_GROUP_ID`, optional): to see joins and
  leaves, so its members are made admins automatically.
- **Felicitation group** (`FELICITATION_GROUP_ID`, optional): to read the
  birthday wishes the weekly brief celebrates.

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
| `LEADERSHIP_GROUP_ID` | No | 0 | Members of this group are automatically admins. Alpha must be an admin there. |
| `FELICITATION_GROUP_ID` | For brief birthdays | 0 | Where birthdays are celebrated; the weekly brief reads it for birthdays only. Alpha must be an admin there. Unset = no birthdays in the brief. |
| `ATL_BACKUP_BUCKET` | For backups | — | S3 bucket used by `scripts/backup_db.py`. |

Blank values are treated as unset. These are fixed in `src/config.py` rather
than read from the environment:

| Setting | Value | What it controls |
|---------|-------|------------------|
| `MAX_DECLINES` | 3 | Declines before each new attempt has to wait `COOLDOWN_SECONDS`. |
| `COOLDOWN_SECONDS` | 6h | The wait between attempts after `MAX_DECLINES`. |
| `INVITE_TTL_SECONDS` | 48h | Lifetime of a main-group invite link. |
| `WELCOME_DELETE_SECONDS` | 24h | Induction welcome message lifetime. |
| `REMINDER_DELETE_SECONDS` | 3h | Induction status replies, and the post they answer. |
| `REGISTRATION_PROMPT_DELETE_SECONDS` | 24h | "Start registration" post, if never tapped. |
| `PROFILE_SESSION_IDLE_SECONDS` | 60 minutes | A quiet fill or edit session is closed. |
| `VOUCH_REMIND_SECONDS` | 12h | How often a silent vouch is reminded. |
| `VOUCH_EXPIRE_SECONDS` | 72h | Vouch silence this long counts as No. |
| `VOUCH_TAG_DELETE_SECONDS` | 6h | Main-group "send me Hi" tag lifetime. |
| `BRIEF_HOUR_UTC`, `BRIEF_LAST_HOUR_UTC` | 9, 21 | The Monday window the weekly brief is posted in. |
| `BRIEF_HIDE_BELOW` | 10 | Week-in-numbers counts below this are left out (0 shows all). |
| `BRIEF_DIGEST_GIVE_UP_DAYS` | 2 | A day whose Gemini digest keeps failing is saved without one. |
| `BRIEF_DIGEST_MAX_CHARS` | 60,000 | Most text from one group's day sent to Gemini (newest kept). |
| `BRIEF_KEEP_DAYS` | 14 | Nothing the brief stores outlives this. |
| `BRIEF_MILESTONES` | ATL Men's Forum, 2020-07-20 | Anniversaries the brief celebrates. |
| `BRIEF_NEVER_READ` | Leadership, onboarding, induction, felicitation | Groups whose text is never stored or sent to Gemini. |

## Running the bot

```bash
python main.py
```

This creates any missing tables (`db.init_db()`), registers the repeating
jobs and every handler, and starts long-polling with
`allowed_updates=Update.ALL_TYPES`, so joins, leaves and join requests are
delivered too. On the server it runs as the `atlbot` systemd service.

Only run **one** instance per `BOT_TOKEN`: Telegram rejects a second
`getUpdates` with a `Conflict` error. Don't start the bot locally with the
production token while the server is running.

On startup it also records the main group as a group the weekly brief reads
(`brief_capture.seed_groups`), once; later changes to that row are kept.

## Tests, CI and deployment

**Run the checks locally** exactly as CI does, from the project root with
`requirements-dev.txt` installed:

```bash
ruff check . --select E4,E7,E9,F    # syntax errors, undefined names, unused imports
python -m compileall -q .
python -m pytest tests
```

Run pytest on `tests` only: `scripts/test_gemini.py` (gitignored) calls the
Gemini API as soon as it is imported, so a bare `pytest` would collect it and
make a real API call. The suite needs no `.env`, database or network today:
`tests/test_validators.py` covers every validator in
`src/kyc_form/validators.py` (text, email, phone, day-month, choice) and the
`validate()` dispatcher. New test files go in `tests/` as `test_*.py`.

**CI/CD** (`.github/workflows/ci.yml`) has two jobs:

1. **test** runs on every pull request and every push to `main`: install
   `requirements-dev.txt` on Python 3.14.4 (kept the same as the server),
   then ruff, compileall and pytest as above.
2. **deploy** runs only on pushes to `main`, only after **test** passes, and
   one at a time (concurrency group `production`; a running deploy is never
   cancelled). Pull requests are tested but never deployed.

**Merging a pull request into `main`, or pushing to it, deploys to
production.** The deploy job SSHes to the EC2 server as `ubuntu`, using three
repository secrets:

| Secret | Holds |
|--------|-------|
| `DEPLOY_SSH_KEY` | Private key for the deploy login. Written to the runner for the job and deleted afterwards, even if the job fails. |
| `EC2_HOST` | The server's address. |
| `EC2_KNOWN_HOSTS` | The server's host key. SSH runs with `StrictHostKeyChecking=yes`, so a server that doesn't match is refused. |

The workflow sends no command: what the deploy login does (update the code,
restart the `atlbot` service) is set on the server, for that key, not in this
repository. The workflow's GitHub token is read-only (`contents: read`).

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
| `profile_sessions` | Members part-way through filling in missing details; `last_active_at` is their last answer. |
| `edit_sessions` | Members part-way through an edit; the draft stays here until they confirm. `last_active_at` is their last answer. |
| `pending_changes` | Profile details waiting for admin approval, and the decision. |
| `main_group_invites` | Each member's current one-time invite link, so it's re-sent rather than re-made. |
| `group_prompts` | When someone was last nudged in the main group to update their details. |
| `vouch_requests` | Each request for a vouch to confirm a member, who the vouch turned out to be, and the outcome (yes, no, expired, not a member, cancelled). |
| `reason_prompts` | Which member an admin's "Other" decline prompt is about, so the prompt text needn't show their id. |
| `special_members` | The owner's Legacy Members: linked by Telegram id, or waiting by username until first seen. |
| `member_managers` | Onboarding leads: who may look members up and expel them. Granted by the owner only, to active members; access needs them still active. |
| `member_roles` | Roles beyond ordinary member (today: admin), and where each came from: the owner (`/admin`) or the leadership group. No row = ordinary member. |
| `brief_groups` | Groups Alpha is in, and whether the weekly brief reads them. |
| `brief_messages` | Group messages waiting to be digested. Deleted once their day is. |
| `brief_days` | Per group per day: message count, who posted, replies received per member, Gemini's digest. Deleted once the week's brief is sent. |
| `brief_birthdays` | Who was wished a happy birthday in the felicitation group, and when. Deleted once the week's brief is sent. |
| `brief_weeks` | One row per weekly brief, so it is never sent twice. |

The database holds real member PII (names, phone numbers, addresses, ID
photos by reference) and is gitignored. It must never be committed.

## Bot commands

| Command | Description |
|---------|-------------|
| `/start` | Deep-link entry point: `?start=kyc` begins or resumes registration, `?start=link` is the "I'm already an ATL member" flow, `?start=vouch` is a named vouch arriving from their main-group tag, and `?start=mentions` opens the weekly-brief naming choice. With no payload, an active member sees their profile. |
| `/profile` | Show your profile, with buttons to fill in or edit details. |
| `/kyc` | Enter registration without the deep link. Marked TEMPORARY. |
| `/chatid` | Replies with the current chat's ID, for filling in `.env`. |
| `/mentions` | In a DM: choose whether the weekly brief may name you. |

Owner only (`OWNER_USER_ID`), in a private chat with Alpha. The commands
are ignored for anyone else:

| Command | Description |
|---------|-------------|
| `/legacy` | Show the **Pick Legacy Members** button; `/legacy @name …` adds by username. |
| `/legacylist` | Who's on the Legacy Members list, and who's still waiting to be seen. |
| `/legacyremove @name` | Take someone off the list. Their membership stays. |
| `/admin` | Show the **Pick admins** button; `/admin @name` for people already on record. |
| `/admins` | List the admins and where each role came from. |
| `/unadmin @name` | Make someone an ordinary member again (not for leadership-group admins). |
| `/askmentions` | Post the "may we name you?" invite, with a button, in the main group. |

## Scripts

Standalone tools in `scripts/`, run from the project root with the venv:

- `import_legacy.py <csv> [--commit]`: one-time import of the old website's
  spreadsheet into `legacy_members`. Dry run by default; the report holds
  counts only, never member data. Uses the same validators and matching keys
  as the live bot.
- `backup_db.py`: hot copy of the database, integrity check, upload to S3,
  keep the newest 14 copies locally.
- `list_model.py`: list the Gemini models available to the API key.
- `stats.py`: a read-only snapshot (members, backlog, admin queue, vouches).
  Counts only, never anyone's details, so its output is safe to share.
- `first_completers.py [--top N] [--since YYYY-MM-DD] [--names-only]
  [--approved-only] [--include-vouch]`: read-only ranking of active members
  by when their last required detail went in, counting profile updates made
  through Alpha since `--since`. A detail counts once saved, approved or
  submitted for approval (`--approved-only`: approved only), and the vouch
  isn't required unless `--include-vouch`. Prints @username or name, never
  Telegram ids; `--names-only` gives a list ready to paste.
- `preview_brief.py --db <live db> [--sample] [--week YYYY-MM-DD] [--no-send]`:
  builds the weekly brief exactly as Monday's job would (real Gemini calls),
  on a throwaway copy of the database, and sends it to the owner's DM only.
  `--sample` adds made-up chat and members so every section shows.

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
- **Tests cover the KYC validators only.** Handlers, the database layer and
  the weekly brief are checked by hand, by ruff and compileall in CI, and for
  the brief by `scripts/preview_brief.py`.
- **Deploys can't be checked from this repository.** What the deploy login
  does is configured on the server, so a green deploy job means the SSH login
  succeeded, not that the new code is running. Check the bot after a merge.
