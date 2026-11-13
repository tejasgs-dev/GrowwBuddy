"""EV-1-11 / EC-ING-12, 19: text clean-up and placeholders."""

from __future__ import annotations

import pytest

from growwbuddy.ingest.parse_groww import format_inr, format_percent, normalise_riskometer
from growwbuddy.ingest.text import clean_text, is_placeholder


def test_EV_1_11_mojibake_and_spaces() -> None:
    assert clean_text("Min SIP â‚¹100 only") == "Min SIP ₹100 only"


def test_strips_html() -> None:
    assert clean_text("<p>Hello <b>world</b></p>") == "Hello world"


@pytest.mark.parametrize("value", [None, "", "NA", "n/a", "–", "--", "0", 0, "  -  "])
def test_placeholders(value: object) -> None:
    assert is_placeholder(value)


@pytest.mark.parametrize("value", ["Nil", "0.74", 100, "Exit load of 1%"])
def test_real_values(value: object) -> None:
    assert not is_placeholder(value)


@pytest.mark.parametrize(
    ("amount", "text"),
    [(100, "₹100"), (1000, "₹1,000"), (100000, "₹1,00,000"), (12345678, "₹1,23,45,678")],
)
def test_format_inr(amount: int, text: str) -> None:
    assert format_inr(amount) == text


def test_format_percent() -> None:
    assert format_percent("1.0") == "1.00%"
    assert format_percent("0.74") == "0.74%"
    assert format_percent("abc") is None


@pytest.mark.parametrize(
    ("raw", "level"),
    [
        ("Very High", "Very High"),
        ("very  high", "Very High"),
        ("Moderately High Riskometer", "Moderately High"),
        ("Low to Moderate Risk", "Low to Moderate"),
        ("Medium", "Medium"),  # unknown passes through; Phase 2 marks it unverified
    ],
)
def test_normalise_riskometer(raw: str, level: str) -> None:
    assert normalise_riskometer(raw) == level
