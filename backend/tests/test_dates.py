"""EV-1-08 / EC-ING-16, 17, 18: date parsing and as-of selection."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from growwbuddy.ingest.dates import choose_as_of, parse_date

FETCHED = datetime(2026, 11, 12, 10, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("31-Oct-2026", date(2026, 10, 31)),
        ("31 Oct 2026", date(2026, 10, 31)),
        ("31/10/2026", date(2026, 10, 31)),
        ("31 October 2026", date(2026, 10, 31)),
        ("2026-11-10T00:00:00", date(2026, 11, 10)),
        ("2026-11-10", date(2026, 11, 10)),
        ("1st Jul 2020", date(2020, 7, 1)),
        ("  11-Nov-2026 ", date(2026, 11, 11)),
        ("03/04/2026", date(2026, 4, 3)),  # Indian DD/MM/YYYY, not 4 March
    ],
)
def test_parse_date_formats(text: str, expected: date) -> None:
    assert parse_date(text) == expected


@pytest.mark.parametrize("text", [None, "", "NA", "--", "soon", "32/13/2026"])
def test_parse_date_rejects_non_dates(text: str | None) -> None:
    assert parse_date(text) is None


def test_choose_page_date() -> None:
    assert choose_as_of(["11-Nov-2026"], FETCHED) == (date(2026, 11, 11), "page")


def test_ist_next_day_allowed() -> None:
    # Page date in IST can be one day ahead of the UTC fetch date.
    assert choose_as_of(["13-Nov-2026"], FETCHED) == (date(2026, 11, 13), "page")


def test_future_date_rejected() -> None:
    assert choose_as_of(["31-Dec-2026"], FETCHED) == (date(2026, 11, 12), "fetched_at")


def test_too_old_rejected() -> None:
    # e.g. a launch date picked up by mistake
    assert choose_as_of(["01-Jan-2013"], FETCHED) == (date(2026, 11, 12), "fetched_at")


def test_first_plausible_candidate_wins() -> None:
    assert choose_as_of([None, "junk", "10-Nov-2026"], FETCHED) == (date(2026, 11, 10), "page")


def test_fallback_when_no_date() -> None:
    assert choose_as_of([], FETCHED) == (date(2026, 11, 12), "fetched_at")
