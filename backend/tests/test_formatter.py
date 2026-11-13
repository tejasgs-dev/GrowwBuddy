"""Formatter (task 3.11; EV-3-18; EC-VAL-10, 11, 12)."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime

import pytest

from growwbuddy.pipeline.formatter import answer_response, footer_date, template_response
from growwbuddy.retrieval.retriever import ContextItem, PageInfo

URL = "https://groww.in/mutual-funds/hdfc-mid-cap-fund-direct-growth"
ITEM = ContextItem(
    item_id="groww-hdfc-mid-cap#expense_ratio",
    kind="chunk",
    scheme="hdfc-mid-cap",
    text="t",
    source_url=URL,
    as_of_date=date(2026, 11, 11),
    fact_as_on=date(2026, 11, 10),
    fetched_at=datetime(2026, 11, 12, 9, 0, tzinfo=UTC),
)


def test_EV_3_18_footer_date_priority() -> None:
    assert footer_date(ITEM) == date(2026, 11, 10)  # the fact's own date
    assert footer_date(replace(ITEM, fact_as_on=None)) == date(2026, 11, 11)  # page date
    no_dates = replace(ITEM, fact_as_on=None, as_of_date=None)
    assert footer_date(no_dates) == date(2026, 11, 12)  # fetched_at (EC-VAL-10)


def test_answer_response() -> None:
    r = answer_response(["The TER is 0.74%.", " It is as on 10 Nov 2026. "], ITEM)
    assert r.type == "answer"
    assert r.text == "The TER is 0.74%. It is as on 10 Nov 2026."
    assert r.citation is not None and r.citation.url == URL
    assert "HDFC Mid Cap Fund" in r.citation.title
    assert r.footer == "Last updated from sources: 10 Nov 2026"  # EC-VAL-11 format
    assert r.last_updated == date(2026, 11, 10)
    assert r.scheme == "hdfc-mid-cap"


class Pages:
    def __init__(self, as_of: date | None = date(2026, 11, 11)) -> None:
        self.as_of = as_of

    def page(self, scheme: str) -> PageInfo | None:
        return PageInfo(scheme, URL, "HDFC Mid Cap Fund Direct Growth – Groww", self.as_of, None)

    def newest_date(self) -> date | None:
        return date(2026, 11, 12)


def test_refusal_links_scheme_page_with_its_date() -> None:
    r = template_response("refusal", "No advice.", scheme="hdfc-mid-cap", pages=Pages())
    assert r.citation is not None and r.citation.url == URL
    assert r.footer == "Last updated from sources: 11 Nov 2026"
    assert r.scheme == "hdfc-mid-cap"


def test_refusal_without_scheme_uses_newest_corpus_date() -> None:
    r = template_response("refusal", "No advice.", pages=Pages())
    assert r.citation is None
    assert r.footer == "Last updated from sources: 12 Nov 2026"  # EC-VAL-12


def test_index_down_drops_the_link() -> None:
    r = template_response("refusal", "x", scheme="hdfc-mid-cap", pages=Pages(as_of=None))
    assert r.citation is None and r.scheme is None


@pytest.mark.parametrize(
    "type_", ["pii_rejected", "greeting", "clarification", "busy", "unavailable"]
)
def test_no_link_types_have_no_link_or_footer(type_: str) -> None:
    r = template_response(type_, "x", scheme="hdfc-mid-cap", pages=Pages())  # type: ignore[arg-type]
    assert r.citation is None and r.footer is None and r.last_updated is None
