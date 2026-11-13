"""PII guard (task 3.1; EV-3-01, 02, 03)."""

from __future__ import annotations

import pytest

from growwbuddy.pipeline.pii_guard import (
    check,
    normalise_digits,
    verhoeff_check_digit,
    verhoeff_valid,
)

_BASE = "23456789012"
AADHAAR = _BASE + verhoeff_check_digit(_BASE)
BAD_AADHAAR = _BASE + str((int(verhoeff_check_digit(_BASE)) + 1) % 10)
SPACED = f"{AADHAAR[:4]} {AADHAAR[4:8]} {AADHAAR[8:]}"
HYPHENATED = f"{AADHAAR[:4]}-{AADHAAR[4:8]}-{AADHAAR[8:]}"

MUST_REJECT: list[tuple[str, str]] = [
    ("ABCDE1234F", "pan"),
    ("abcde1234f", "pan"),
    ("ABCDE 1234 F", "pan"),
    (f"my aadhaar is {AADHAAR}", "aadhaar"),
    (f"aadhaar {SPACED}", "aadhaar"),
    (f"aadhaar {HYPHENATED}", "aadhaar"),
    ("+91 98765 43210", "phone"),
    ("098765-43210", "phone"),
    ("a.b@example.com", "email"),
    ("name at example dot com", "email"),
    ("My OTP is 482913", "otp"),
    # Golden item G-PII-005 (Phase 6): a comma or full stop after the OTP hid it.
    ("My OTP is 482913, please complete my SIP", "otp"),
    ("my otp is 482913.", "otp"),
    ("folio 12345678/90", "account"),
    ("account no 123456789012", "account"),
    ("call me on ९८७६५४३२१०", "phone"),  # Devanagari digits (EC-PII-13)
]

MUST_ALLOW = [
    "Is PAN needed to invest?",
    f"what does {BAD_AADHAAR} mean",
    "₹500 SIP",
    "₹1000000000 corpus",
    "in 2026",
    "for 3 years",
    "1% exit load",
    "Scheme code 118989",
    "PIN code 560001",
    "What is the exit load of HDFC Mid Cap Fund?",
    "Minimum SIP for small cap is ₹100?",
]


def test_verhoeff_helpers() -> None:
    assert verhoeff_valid(AADHAAR)
    assert not verhoeff_valid(BAD_AADHAAR)


@pytest.mark.parametrize(("text", "kind"), MUST_REJECT)
def test_EV_3_01_pii_detected(text: str, kind: str) -> None:
    result = check(text)
    assert result.found
    assert kind in result.types


@pytest.mark.parametrize("text", MUST_ALLOW)
def test_EV_3_02_no_false_positives(text: str) -> None:
    assert check(text).types == ()


@pytest.mark.parametrize(("text", "_kind"), MUST_REJECT)
def test_EV_3_03_result_holds_types_only(text: str, _kind: str) -> None:
    result = check(text)
    # Nothing in the result can carry the matched value.
    assert set(result.types) <= {"pan", "aadhaar", "phone", "email", "otp", "account"}
    assert all(v not in repr(result) for v in ("1234", "98765", "example", "482913"))


def test_normalise_digits() -> None:
    assert normalise_digits("९८७६५") == "98765"
    assert normalise_digits("１２３") == "123"
