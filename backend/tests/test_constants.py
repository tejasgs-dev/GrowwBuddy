"""EV-0-07: shared constants."""

from __future__ import annotations

from datetime import date

from growwbuddy import constants


def test_EV_0_07_disclaimer() -> None:
    assert constants.DISCLAIMER == "Facts-only. No investment advice."


def test_EV_0_07_scheme_keys() -> None:
    assert constants.SCHEME_KEYS == (
        "hdfc-mid-cap",
        "hdfc-small-cap",
        "hdfc-large-cap",
        "hdfc-silver-etf-fof",
        "hdfc-multi-cap",
    )
    assert set(constants.SCHEME_NAMES) == set(constants.SCHEME_KEYS)


def test_EV_0_07_allowed_domain() -> None:
    assert constants.ALLOWED_DOMAIN == "groww.in"


def test_EV_0_07_footer_format() -> None:
    assert constants.format_footer(date(2026, 10, 31)) == "Last updated from sources: 31 Oct 2026"
    assert constants.format_footer(date(2026, 3, 4)) == "Last updated from sources: 04 Mar 2026"
