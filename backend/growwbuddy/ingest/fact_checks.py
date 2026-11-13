"""Sanity checks for scheme facts (task 2.9, §5.3).

Each check returns None when the value passes, or a short reason when it doesn't. A fact
that fails is stored with `verified = false` and kept out of answers until a person reviews
it (EC-RET-05). Boundaries are inclusive (EC-FCT-09).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from decimal import Decimal, InvalidOperation

from growwbuddy.constants import RISKOMETER_LEVELS
from growwbuddy.ingest.text import clean_text, is_placeholder

TER_MIN = Decimal("0.01")
TER_MAX = Decimal("3.00")

_PERCENT = re.compile(r"^(\d+(?:\.\d+)?)\s*%$")
_RUPEES = re.compile(r"^₹\s?(\d{1,3}(?:,\d{2})*(?:,\d{3})|\d+)$")
_LOCK_IN = re.compile(r"^\d+ (?:year|month|day)s?(?: \d+ (?:year|month|day)s?)*$")

Check = Callable[[str], str | None]


def _placeholder(value: str) -> str | None:
    return "missing or placeholder value" if is_placeholder(value) else None


def parse_percent(value: str) -> Decimal | None:
    m = _PERCENT.match(clean_text(value))
    if not m:
        return None
    try:
        return Decimal(m.group(1))
    except InvalidOperation:
        return None


def parse_rupees(value: str) -> int | None:
    m = _RUPEES.match(clean_text(value))
    return int(m.group(1).replace(",", "")) if m else None


def check_expense_ratio(value: str) -> str | None:
    if err := _placeholder(value):
        return err
    pct = parse_percent(value)
    if pct is None:
        return f"not a percentage: {value!r}"
    if not TER_MIN <= pct <= TER_MAX:
        return f"TER {pct}% outside {TER_MIN}-{TER_MAX}%"
    return None


def check_rupee_amount(value: str) -> str | None:
    if err := _placeholder(value):
        return err
    amount = parse_rupees(value)
    if amount is None:
        return f"not a rupee amount: {value!r}"
    if amount <= 0:
        return "amount must be positive"
    return None


def check_riskometer(value: str) -> str | None:
    if err := _placeholder(value):
        return err
    if value not in RISKOMETER_LEVELS:
        return f"{value!r} is not one of the six SEBI riskometer levels"
    return None


def check_lock_in(value: str) -> str | None:
    if err := _placeholder(value):
        return err
    return None if _LOCK_IN.match(value) else f"unexpected lock-in format: {value!r}"


def check_text(value: str) -> str | None:
    """Free-text facts (benchmark, fund manager): present and not a placeholder."""
    if err := _placeholder(value):
        return err
    return "too short" if len(clean_text(value)) < 3 else None


def check_exit_load(value: str) -> str | None:
    """'Nil' is a real exit load (EC-FCT-02); any stated percentage must be 0-5%."""
    if err := _placeholder(value):
        return err
    for pct in re.findall(r"(\d+(?:\.\d+)?)\s*%", value):
        if Decimal(pct) > 5:
            return f"exit load {pct}% looks wrong (over 5%)"
    return None


CHECKS: dict[str, Check] = {
    "expense_ratio_direct": check_expense_ratio,
    "expense_ratio_base": check_expense_ratio,
    "exit_load": check_exit_load,
    "min_sip": check_rupee_amount,
    "min_lumpsum": check_rupee_amount,
    "benchmark": check_text,
    "riskometer": check_riskometer,
    "lock_in": check_lock_in,
    "fund_manager": check_text,
}


def check_fact(fact_key: str, value: str | None) -> str | None:
    if value is None:
        return "no value extracted"
    return CHECKS[fact_key](value)
