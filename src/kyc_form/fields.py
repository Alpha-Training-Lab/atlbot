"""Single source of truth for what Alpha asks during KYC.

Adding a field = add a dict here. Removing = set "active": False
(keeps answers already collected). No schema migration either way.

"edit" controls the member's profile:
  "self"     - saved as soon as the member types it
  "approval" - held until an admin approves it (identity and vouch details)
A field with no "edit" key is treated as "approval", the safe default.
"""
from datetime import datetime

KYC_FIELDS = [
    {
        "key": "full_name",
        "label": "Full name", "edit": "approval",
        "prompt": "What is your full name, exactly as it appears on your ID?",
        "type": "text",
        "required": True, "active": True, "order": 10,
    },
    {
        "key": "email",
        "label": "Email", "edit": "self",
        "prompt": "What's your email address?",
        "type": "email",
        "required": True, "active": True, "order": 20,
    },
    {
        "key": "newsletter_opt_in",
        "label": "Newsletter", "edit": "self",
        "prompt": "Would you like to receive the ATL newsletter by email?",
        "type": "choice",
        "options": ["Yes, sign me up", "No thanks"],
        "required": False, "active": False, "order": 25,
    },
    {
        "key": "mobile",
        "label": "Phone number", "edit": "self",
        "prompt": "What's your phone number? Include the country code, e.g. +234...",
        "type": "phone",
        "required": True, "active": True, "order": 30,
    },
    {
        "key": "birthday",
        "label": "Birthday", "edit": "approval",
        "prompt": "What day and month is your birthday? e.g. 14 March\n"
                  "(We only keep the day and month — never the year.)",
        "type": "day_month",
        "required": True, "active": True, "order": 40,
    },
    {
        "key": "gender",
        "label": "Gender", "edit": "self",
        "prompt": "What is your gender?",
        "type": "choice",
        "options": ["Male", "Female", "Prefer not to say"],
        "required": True, "active": True, "order": 50,
    },
    {
        "key": "country_of_residence",
        "label": "Country", "edit": "self",
        "prompt": "Which country do you currently live in?",
        "type": "text",
        "required": True, "active": True, "order": 60,
    },
    {
        "key": "state",
        "label": "State / region", "edit": "self",
        "prompt": "Which state or region?",
        "type": "text",
        "required": True, "active": True, "order": 70,
    },
    {
        "key": "address",
        "label": "Address", "edit": "self",
        "prompt": "What is your residential address?",
        "type": "text",
        "required": True, "active": True, "order": 80,
    },
    {
        "key": "referral_source",
        "label": "How you heard about ATL", "edit": "self",
        "prompt": "How did you hear about ATL?",
        "type": "text",
        "required": True, "active": True, "order": 90,
    },
    {
        "key": "vouch_name",
        "label": "Vouch's name", "edit": "approval",
        "prompt": "What is the full name of the person who introduced you to ATL?",
        "type": "text",
        "required": True, "active": True, "order": 100,
    },
    {
        "key": "vouch_username",
        "label": "Vouch's username", "edit": "approval",
        "prompt": "What is their Telegram username? e.g. @username",
        "type": "text",
        "required": True, "active": True, "order": 110,
    },
    {
        "key": "id_type",
        "label": "ID type", "edit": "approval",
        "prompt": "Which ID will you be uploading?",
        "type": "choice",
        "options": ["NIN Slip", "International Passport",
                    "Driver's Licence", "Voter's Card"],
        "required": True, "active": True, "order": 120,
    },
    {
        "key": "id_document",
        "label": "ID document", "edit": "approval",
        "prompt": "Please upload a clear photo of that ID.",
        "type": "document",
        "required": True, "active": True, "order": 130,
    },
    {
        "key": "id_with_face",
        "label": "Photo holding ID", "edit": "approval",
        "prompt": "Now upload a photo of yourself holding that same ID.",
        "type": "document",
        "required": True, "active": True, "order": 140,
    },
]


def active_fields():
    """Ordered list of fields currently being asked."""
    return sorted(
        (f for f in KYC_FIELDS if f["active"]),
        key=lambda f: f["order"],
    )


def field_by_key(key):
    for f in KYC_FIELDS:
        if f["key"] == key:
            return f
    return None


def needs_approval(field):
    return field.get("edit", "approval") != "self"


def label(field):
    return field.get("label") or field["key"].replace("_", " ").capitalize()


def display_value(field, value_text, file_ref=None):
    """A stored answer as people should read it. Birthdays are stored as
    MM-DD, which reads as the wrong date day-first ("07-05" is 5 July, not
    7 May), so they're spelled out."""
    if file_ref:
        return "on file"
    value = value_text or "—"
    if field and field["type"] == "day_month" and len(value) == 5:
        try:
            dt = datetime.strptime(f"2000-{value}", "%Y-%m-%d")
            return f"{dt.day} {dt.strftime('%B')}"
        except ValueError:
            pass
    return value