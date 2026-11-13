"""PII guard: deterministic detectors that run before anything is stored, logged or sent to
the model (task 3.1, architecture §6.1).

`check()` returns only the *types* that matched, never the matched text, so callers can't
log a value by accident (EC-PII-17).
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Literal

PiiType = Literal["pan", "aadhaar", "phone", "email", "otp", "account"]

# ---------------------------------------------------------------- normalisation


def normalise_digits(text: str) -> str:
    """Devanagari, full-width and other decimal digits -> ASCII (EC-PII-13)."""
    out = []
    for ch in text:
        if ch.isdigit() and not ch.isascii():
            try:
                out.append(str(unicodedata.decimal(ch)))
                continue
            except ValueError:
                pass
        out.append(ch)
    return unicodedata.normalize("NFKC", "".join(out))


# ---------------------------------------------------------------- Verhoeff (Aadhaar)

_D = (
    (0, 1, 2, 3, 4, 5, 6, 7, 8, 9),
    (1, 2, 3, 4, 0, 6, 7, 8, 9, 5),
    (2, 3, 4, 0, 1, 7, 8, 9, 5, 6),
    (3, 4, 0, 1, 2, 8, 9, 5, 6, 7),
    (4, 0, 1, 2, 3, 9, 5, 6, 7, 8),
    (5, 9, 8, 7, 6, 0, 4, 3, 2, 1),
    (6, 5, 9, 8, 7, 1, 0, 4, 3, 2),
    (7, 6, 5, 9, 8, 2, 1, 0, 4, 3),
    (8, 7, 6, 5, 9, 3, 2, 1, 0, 4),
    (9, 8, 7, 6, 5, 4, 3, 2, 1, 0),
)
_P = (
    (0, 1, 2, 3, 4, 5, 6, 7, 8, 9),
    (1, 5, 7, 6, 2, 8, 3, 0, 9, 4),
    (5, 8, 0, 3, 7, 9, 6, 1, 4, 2),
    (8, 9, 1, 6, 0, 4, 3, 5, 2, 7),
    (9, 4, 5, 3, 1, 2, 6, 8, 7, 0),
    (4, 2, 8, 6, 5, 7, 3, 9, 0, 1),
    (2, 7, 9, 3, 8, 0, 6, 4, 1, 5),
    (7, 0, 4, 6, 9, 1, 3, 2, 5, 8),
)
_INV = (0, 4, 3, 2, 1, 5, 6, 7, 8, 9)


def verhoeff_valid(number: str) -> bool:
    c = 0
    for i, ch in enumerate(reversed(number)):
        c = _D[c][_P[i % 8][int(ch)]]
    return c == 0


def verhoeff_check_digit(number: str) -> str:
    """The digit that makes `number + digit` Verhoeff-valid (used by tests)."""
    c = 0
    for i, ch in enumerate(reversed(number)):
        c = _D[c][_P[(i + 1) % 8][int(ch)]]
    return str(_INV[c])


# ---------------------------------------------------------------- detectors

# PAN: case-insensitive when written solid; with spaces only in capitals, so ordinary text
# like "small 2026 a" isn't mistaken for one.
_PAN = re.compile(r"(?<![a-z0-9])[a-z]{5}\d{4}[a-z](?![a-z0-9])", re.IGNORECASE)
_PAN_SPACED = re.compile(r"(?<![A-Za-z0-9])[A-Z]{5}\s\d{4}\s?[A-Z](?![A-Za-z0-9])")
_AADHAAR = re.compile(r"(?<![\d])([2-9]\d{3})[\s-]?(\d{4})[\s-]?(\d{4})(?![\d])")
_PHONE = re.compile(r"(?<![\d+])(?:\+?91[\s-]?|0)?([6-9]\d{4})[\s-]?(\d{5})(?![\d])")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", re.IGNORECASE)
_EMAIL_DISGUISED = re.compile(
    r"\b[\w.+-]+\s*(?:\(at\)|\[at\]|\sat\s)\s*[\w-]+\s*(?:\(dot\)|\[dot\]|\sdot\s)\s*"
    r"(?:com|in|org|net|co|io|edu|gov)\b",
    re.IGNORECASE,
)
_AMOUNT_BEFORE = re.compile(r"(?:₹|rs\.?|inr)\s*$", re.IGNORECASE)
_AMOUNT_AFTER = re.compile(r"^\s*(?:rupees|rs\b|inr|lakh|lakhs|crore|crores|/-)", re.IGNORECASE)

# OTP: a 4-8 digit number near an OTP word. "Scheme code", "AMFI code" and postal
# "PIN code" are not OTPs (EC-PII-10).
_OTP_WORD = re.compile(
    r"\b(?:otp|one[\s-]?time\s+password|verification\s+code|security\s+code|login\s+code|"
    r"passcode|pin(?!\s*code))\b",
    re.IGNORECASE,
)
# A separator only rules a number out when a digit is on its other side ("0.74", "1,000"),
# so "my OTP is 482913, please..." and "...482913." are still caught.
_SHORT_NUMBER = re.compile(r"(?<!\d)(?<!\d[.,])\d{4,8}(?![\d%])(?![.,]\d)")

# Account or folio number: 6+ digits (slashes and hyphens allowed) near the keyword.
_ACCOUNT = re.compile(
    r"\b(?:folio|account|a/c|acct|acc)\b(?:\s*(?:no\.?|number|num|#|:))?\D{0,12}?"
    r"(\d[\d/\-\s]{4,24}\d)",
    re.IGNORECASE,
)

_NEAR = 30  # characters either side for keyword-based detectors


@dataclass(frozen=True)
class PiiResult:
    types: tuple[PiiType, ...]

    @property
    def found(self) -> bool:
        return bool(self.types)


def _is_amount(text: str, start: int, end: int) -> bool:
    return bool(_AMOUNT_BEFORE.search(text[max(0, start - 6) : start])) or bool(
        _AMOUNT_AFTER.search(text[end : end + 12])
    )


def _has_aadhaar(text: str) -> bool:
    for m in _AADHAAR.finditer(text):
        digits = "".join(m.groups())
        if verhoeff_valid(digits) and not _is_amount(text, m.start(), m.end()):
            return True
    return False


def _has_phone(text: str) -> bool:
    return any(not _is_amount(text, m.start(), m.end()) for m in _PHONE.finditer(text))


def _has_otp(text: str) -> bool:
    for m in _SHORT_NUMBER.finditer(text):
        window = text[max(0, m.start() - _NEAR) : m.end() + _NEAR]
        if _OTP_WORD.search(window):
            return True
    return False


def _has_account(text: str) -> bool:
    return any(sum(ch.isdigit() for ch in m.group(1)) >= 6 for m in _ACCOUNT.finditer(text))


def check(text: str) -> PiiResult:
    t = normalise_digits(text)
    found: list[PiiType] = []
    if _PAN.search(t) or _PAN_SPACED.search(t):
        found.append("pan")
    if _has_aadhaar(t):
        found.append("aadhaar")
    if _has_phone(t):
        found.append("phone")
    if _EMAIL.search(t) or _EMAIL_DISGUISED.search(t):
        found.append("email")
    if _has_otp(t):
        found.append("otp")
    if _has_account(t):
        found.append("account")
    return PiiResult(tuple(found))
