"""The KYC form: which questions Alpha asks (fields.py) and how answers are
checked and cleaned (validators.py). Shared by registration (onboarding),
the member profile (members) and scripts/import_legacy.py."""
from src.kyc_form.fields import (
  KYC_FIELDS, active_fields, display_value, field_by_key, label, needs_approval,
)
from src.kyc_form.validators import validate

__all__ = [
  "KYC_FIELDS", "active_fields", "display_value", "field_by_key", "label",
  "needs_approval", "validate",
]
