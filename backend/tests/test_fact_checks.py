"""EV-2-05, EV-2-06: fact sanity checks (task 2.9)."""

from __future__ import annotations

import pytest

from growwbuddy.ingest.fact_checks import check_fact
from growwbuddy.ingest.parse_groww import normalise_riskometer


@pytest.mark.parametrize(
    ("value", "ok"),
    [
        ("0.74%", True),
        ("0.01%", True),  # lower boundary, inclusive (EC-FCT-09)
        ("3.00%", True),  # upper boundary, inclusive
        ("3.01%", False),
        ("0%", False),
        ("0", False),  # placeholder (EC-ING-12)
        ("NA", False),
        ("–", False),
        ("--", False),
        ("about 1", False),
    ],
)
def test_EV_2_05_expense_ratio(value: str, ok: bool) -> None:
    assert (check_fact("expense_ratio_direct", value) is None) is ok


@pytest.mark.parametrize(
    ("value", "ok"),
    [
        ("₹100", True),
        ("₹1,00,000", True),
        ("₹5000", True),
        ("₹0", False),
        ("100", False),  # not a rupee amount
        ("NA", False),
        ("₹-100", False),
    ],
)
@pytest.mark.parametrize("key", ["min_sip", "min_lumpsum"])
def test_EV_2_05_rupee_amounts(key: str, value: str, ok: bool) -> None:
    assert (check_fact(key, value) is None) is ok


@pytest.mark.parametrize(
    ("value", "ok"),
    [
        ("Exit load of 1% if redeemed within 1 year", True),
        ("Nil", True),  # a real value (EC-FCT-02)
        ("NA", False),
        ("Exit load of 15% if redeemed within 1 year", False),
    ],
)
def test_EV_2_05_exit_load(value: str, ok: bool) -> None:
    assert (check_fact("exit_load", value) is None) is ok


@pytest.mark.parametrize(
    ("key", "value", "ok"),
    [
        ("lock_in", "3 years", True),
        ("lock_in", "about three years", False),
        ("benchmark", "NIFTY Midcap 150 Total Return Index", True),
        ("benchmark", "NA", False),
        ("fund_manager", "Chirag Setalvad, Dhruv Muchhal", True),
        ("fund_manager", "–", False),
    ],
)
def test_EV_2_05_other_checks(key: str, value: str, ok: bool) -> None:
    assert (check_fact(key, value) is None) is ok


def test_no_value_fails() -> None:
    assert check_fact("benchmark", None) is not None


@pytest.mark.parametrize(
    ("raw", "ok"),
    [
        ("Very high", True),
        ("very  High", True),
        ("Moderately High", True),
        ("Very High Risk", True),
        ("Medium", False),
    ],
)
def test_EV_2_06_riskometer(raw: str, ok: bool) -> None:
    assert (check_fact("riskometer", normalise_riskometer(raw)) is None) is ok
