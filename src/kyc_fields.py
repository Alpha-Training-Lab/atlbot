"""Single source of truth for what Alpha asks during KYC.

Adding a field = add a dict here. Removing = set "active": False
(keeps answers already collected). No schema migration either way.
"""

KYC_FIELDS = [
    {
        "key": "full_name",
        "prompt": "What is your full name, exactly as it appears on your ID?",
        "type": "text",
        "required": True, "active": True, "order": 10,
    },
    {
        "key": "email",
        "prompt": "What's your email address?",
        "type": "email",
        "required": True, "active": True, "order": 20,
    },
    {
        "key": "newsletter_opt_in",
        "prompt": "Would you like to receive the ATL newsletter by email?",
        "type": "choice",
        "options": ["Yes, sign me up", "No thanks"],
        "required": False, "active": False, "order": 25,
    },
    {
        "key": "mobile",
        "prompt": "What's your phone number? Include the country code, e.g. +234...",
        "type": "phone",
        "required": True, "active": True, "order": 30,
    },
    {
        "key": "birthday",
        "prompt": "What day and month is your birthday? e.g. 14 March\n"
                  "(We only keep the day and month — never the year.)",
        "type": "day_month",
        "required": True, "active": True, "order": 40,
    },
    {
        "key": "gender",
        "prompt": "What is your gender?",
        "type": "choice",
        "options": ["Male", "Female", "Prefer not to say"],
        "required": True, "active": True, "order": 50,
    },
    {
        "key": "country_of_residence",
        "prompt": "Which country do you currently live in?",
        "type": "text",
        "required": True, "active": True, "order": 60,
    },
    {
        "key": "state",
        "prompt": "Which state or region?",
        "type": "text",
        "required": True, "active": True, "order": 70,
    },
    {
        "key": "address",
        "prompt": "What is your residential address?",
        "type": "text",
        "required": True, "active": True, "order": 80,
    },
    {
        "key": "referral_source",
        "prompt": "How did you hear about ATL?",
        "type": "text",
        "required": True, "active": True, "order": 90,
    },
    {
        "key": "vouch_name",
        "prompt": "What is the full name of the person who introduced you to ATL?",
        "type": "text",
        "required": True, "active": True, "order": 100,
    },
    {
        "key": "vouch_username",
        "prompt": "What is their Telegram username? e.g. @username",
        "type": "text",
        "required": True, "active": True, "order": 110,
    },
    {
        "key": "id_type",
        "prompt": "Which ID will you be uploading?",
        "type": "choice",
        "options": ["NIN Slip", "International Passport",
                    "Driver's Licence", "Voter's Card"],
        "required": True, "active": True, "order": 120,
    },
    {
        "key": "id_document",
        "prompt": "Please upload a clear photo of that ID.",
        "type": "document",
        "required": True, "active": True, "order": 130,
    },
    {
        "key": "id_with_face",
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


def total_active():
    return len(active_fields())
