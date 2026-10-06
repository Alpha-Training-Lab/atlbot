"""Tests for src/kyc_form/validators.py.

Every validator returns (ok, cleaned, error):
  valid   -> (True, cleaned_value, None)
  invalid -> (False, None, error_message)
"""
import pytest

from src.kyc_form.validators import (
  validate,
  validate_choice,
  validate_day_month,
  validate_email,
  validate_phone,
  validate_text,
)


# ---------- text ----------

def test_text_valid_is_stripped():
  assert validate_text("  Lagos  ") == (True, "Lagos", None)


@pytest.mark.parametrize("raw", ["", "a", "   ", None, "x" * 501])
def test_text_invalid(raw):
  ok, cleaned, error = validate_text(raw)
  assert not ok
  assert cleaned is None
  assert error


# ---------- email ----------

def test_email_strips_whitespace():
  assert validate_email("  sam@example.com ") == (True, "sam@example.com", None)


def test_email_is_lowercased():
  assert validate_email("Sam@Example.COM") == (True, "sam@example.com", None)


@pytest.mark.parametrize("raw", ["", "sam", "sam@", "sam@example", "sam @example.com"])
def test_email_invalid(raw):
  ok, cleaned, error = validate_email(raw)
  assert not ok
  assert cleaned is None
  assert error


# ---------- phone ----------

@pytest.mark.parametrize("raw, expected", [
  ("+2348031234567", "+2348031234567"),
  ("+234 803 123 4567", "+2348031234567"),
  ("(0803) 123-4567", "08031234567"),
])
def test_phone_valid(raw, expected):
  assert validate_phone(raw) == (True, expected, None)


@pytest.mark.parametrize("raw", ["", "12345", "phone", "+234803123456789012"])
def test_phone_invalid(raw):
  ok, cleaned, error = validate_phone(raw)
  assert not ok
  assert cleaned is None
  assert error


# ---------- day_month (returns MM-DD) ----------

@pytest.mark.parametrize("raw, expected", [
  ("05/07", "07-05"),        # day first: 5 July, not 7 May
  ("14 March", "03-14"),
  ("14 Mar", "03-14"),
  ("March 14", "03-14"),
  ("29/02", "02-29"),        # leap day accepted
  ("05/07/1990", "07-05"),   # year is discarded
  ("1990-07-05", "07-05"),
])
def test_day_month_valid(raw, expected):
  assert validate_day_month(raw) == (True, expected, None)


@pytest.mark.parametrize("raw", ["31/02", "32/01", "13/13", "tomorrow", ""])
def test_day_month_invalid(raw):
  ok, cleaned, error = validate_day_month(raw)
  assert not ok
  assert cleaned is None
  assert error


# ---------- choice ----------

def test_choice_is_case_insensitive_and_returns_canonical_option():
  assert validate_choice(" yes ", ["Yes", "No"]) == (True, "Yes", None)


def test_choice_invalid():
  ok, cleaned, error = validate_choice("maybe", ["Yes", "No"])
  assert not ok
  assert cleaned is None
  assert error


# ---------- validate() dispatcher ----------

def test_dispatch_routes_by_field_type():
  assert validate({"type": "email"}, "A@B.com") == (True, "a@b.com", None)


def test_dispatch_choice_uses_field_options():
  field = {"type": "choice", "options": ["Male", "Female"]}
  assert validate(field, "female") == (True, "Female", None)


def test_dispatch_unknown_type_falls_back_to_text():
  assert validate({"type": "unknown"}, "  hi  ") == (True, "hi", None)