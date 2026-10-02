"""What Alpha is told about the member it's talking to, so its answers fit
where they are in the journey. Used for DMs (chat.py) and for public replies
in the induction group (onboarding/induction.py)."""
from src import db
# =============================================================================

STATUS_CONTEXT = {
  db.STATUS_PENDING_SUMMARY:
    "This member has NOT yet been approved out of the induction group. "
    "Their next step is to finish reading the induction material, then "
    "post in the induction group tagging the onboarding admins. "
    "Registration cannot begin until an admin approves them. "
    "BUT if they say they are already in the main ATL group, believe that "
    "they may be right: tell them to tap the 'I'm already an ATL member' "
    "button below, which checks with Telegram and recognises them.",
  db.STATUS_PENDING_REVIEW:
    "This member has posted in the induction group and an admin is "
    "reviewing them. They only need to wait.",
  db.STATUS_AWAITING_DM:
    "This member has been approved out of induction. They should tap the "
    "button in the induction group to start registration with you.",
  db.STATUS_KYC_IN_PROGRESS:
    "This member is part-way through registration. Tell them to answer "
    "the question you last asked.",
  db.STATUS_PENDING_ACCESS:
    "This member has finished registration. The admin team is doing the "
    "final review. They only need to wait.",
  db.STATUS_ACTIVE:
    "This member is fully approved and has group access. If they want to "
    "see, complete or update their personal details, tell them to tap the "
    "My profile button below or send /profile. Never ask them to type "
    "personal details to you in this chat.",
  db.STATUS_DECLINED:
    "This member's registration was declined. They can fix the issue and "
    "submit again using the button already sent to them.",
}

NO_RECORD = (
  "This person has no record in the ATL database. If they say they are "
  "already an ATL member, tell them to tap the 'I'm already an ATL member' "
  "button below so you can find their record. Otherwise they have not "
  "started onboarding, and their first step is the induction group."
)


def context_for(member):
  if member is None:
    return NO_RECORD
  return STATUS_CONTEXT.get(member["status"], NO_RECORD)
