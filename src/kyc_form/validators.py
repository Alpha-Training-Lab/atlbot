"""Validation + normalisation for KYC answers.

Contract:  validate(field, raw) -> (ok: bool, cleaned: str|None, error: str|None)
"""
import re
from datetime import datetime
# =====================================

# Deliberately loose: catches typos, not a proof of deliverability.
# The only real email validation is sending one.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[a-zA-Z]{2,}$")

_NO_YEAR_FORMATS   = ["%d %B", "%d %b", "%B %d", "%b %d", "%d/%m", "%d-%m"]
_WITH_YEAR_FORMATS = ["%d %B %Y", "%d %b %Y", "%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d"]


def _clean(raw):
  return (raw or "").strip()


def validate_text(raw):
  v = _clean(raw)
  if len(v) < 2:
    return False, None, "That looks too short. Please type it in full."
  if len(v) > 500:
    return False, None, "That's too long — please keep it under 500 characters."
  return True, v, None


def validate_email(raw):
  v = _clean(raw).lower()
  if not _EMAIL_RE.match(v):
    return False, None, "That doesn't look like an email address. Example: name@example.com"
  return True, v, None


def validate_phone(raw):
  v = re.sub(r"[\s\-\(\)\.]", "", _clean(raw))
  if not re.match(r"^\+?\d{7,15}$", v):
    return False, None, ("Please send digits only, with the country code. "
                         "Example: +2348031234567")
  return True, v, None


def validate_day_month(raw):
  """Return MM-DD. The year is discarded even if supplied."""
  v = _clean(raw)

  for fmt in _WITH_YEAR_FORMATS:
    try:
      dt = datetime.strptime(v, fmt)
      return True, f"{dt.month:02d}-{dt.day:02d}", None
    except ValueError:
      pass

  # 2000 is a leap year, so 29 February parses correctly.
  for fmt in _NO_YEAR_FORMATS:
    try:
      dt = datetime.strptime(f"{v} 2000", fmt + " %Y")
      return True, f"{dt.month:02d}-{dt.day:02d}", None
    except ValueError:
      pass

  return False, None, ("I couldn't read that date. Try it like this: 14 March")


def validate_choice(raw, options):
  v = _clean(raw)
  for opt in options:
    if v.lower() == opt.lower():
      return True, opt, None
  return False, None, "Please tap one of the buttons above."


_DISPATCH = {
  "text":  lambda f, r: validate_text(r),
  "email": lambda f, r: validate_email(r),
  "phone": lambda f, r: validate_phone(r),
  "day_month": lambda f, r: validate_day_month(r),
  "choice": lambda f, r: validate_choice(r, f.get("options", [])),
}


def validate(field, raw):
  fn = _DISPATCH.get(field["type"])
  if fn is None:
    return validate_text(raw)   # safe default
  return fn(field, raw)