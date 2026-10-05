from src.kyc_form.validators import validate_email


def test_email_strips_whitespace():
  ok, cleaned, error = validate_email("  sam@example.com ")
  assert ok
  assert cleaned == "sam@example.com"
  assert error is None