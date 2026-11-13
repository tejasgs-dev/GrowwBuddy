"""Facts review tool (task 2.10, EV-2-10, EV-2-12 tooling)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from growwbuddy.facts.review import (
    ReviewError,
    describe,
    list_facts,
    review_loop,
    set_manual,
    verify_fact,
)
from growwbuddy.ingest.index import build_index
from growwbuddy.ingest.parse_groww import Fact, ParsedPage
from growwbuddy.retrieval.headers import RetrievalHeaders
from growwbuddy.store.models import SchemeFact, SchemeFactHistory
from tests.fakes import FailIfCalledLLM, FakeEmbedder

NOW = datetime(2026, 11, 12, 12, 0, tzinfo=UTC)


@pytest.fixture
def sf(
    parsed_pages: list[ParsedPage],
    session_factory: sessionmaker[Session],
    retrieval_headers: RetrievalHeaders,
) -> sessionmaker[Session]:
    # Make one fact conflict, so there is something unverified to review (EC-FCT-10).
    mid = parsed_pages[0]
    fields = dict(mid.fields)
    fields["expense_ratio_history"] = Fact(value="0.77%", as_on=fields["expense_ratio"].as_on)
    pages = [mid.model_copy(update={"fields": fields}), *parsed_pages[1:]]
    build_index(
        pages,
        session_factory=session_factory,
        embedder=FakeEmbedder(),
        headers=retrieval_headers,
        llm=FailIfCalledLLM(),
    )
    return session_factory


def test_EV_2_10_review_tool_shows_both_values(sf: sessionmaker[Session]) -> None:
    with sf() as s:
        rows = list_facts(s, unverified_only=True)
    assert [(r.scheme, r.fact_key) for r in rows] == [("hdfc-mid-cap", "expense_ratio_direct")]
    text = describe(rows[0])
    assert "UNVERIFIED" in text and "0.74%" in text and "0.77%" in text


def test_pending_lists_everything_not_yet_reviewed(sf: sessionmaker[Session]) -> None:
    with sf() as s:
        assert len(list_facts(s, pending_only=True)) == 40
        verify_fact(s, "hdfc-mid-cap", "benchmark", "tester", NOW)
        s.commit()
        assert len(list_facts(s, pending_only=True)) == 39


def test_verify_records_reviewer_and_date(sf: sessionmaker[Session]) -> None:
    with sf() as s:
        verify_fact(s, "hdfc-mid-cap", "expense_ratio_direct", "tester", NOW)
        s.commit()
        row = s.get(SchemeFact, ("hdfc-mid-cap", "expense_ratio_direct"))
    assert row is not None and row.verified and row.check_error is None
    assert row.reviewed_by == "tester"
    assert row.reviewed_at is not None and row.reviewed_at.replace(tzinfo=UTC) == NOW


def test_set_manual_value(sf: sessionmaker[Session]) -> None:
    with sf() as s:
        set_manual(s, "hdfc-mid-cap", "expense_ratio_direct", "0.77%", "tester", NOW)
        s.commit()
        row = s.get(SchemeFact, ("hdfc-mid-cap", "expense_ratio_direct"))
        hist = list(s.scalars(select(SchemeFactHistory)))
    assert row is not None
    assert (row.value_text, row.extracted_by, row.verified) == ("0.77%", "manual", True)
    assert row.extracted_value == "0.74%"  # what the extractor saw is kept
    assert [(h.old_value, h.new_value) for h in hist] == [("0.74%", "0.77%")]


def test_manual_value_survives_reingest(
    sf: sessionmaker[Session],
    parsed_pages: list[ParsedPage],
    retrieval_headers: RetrievalHeaders,
) -> None:
    with sf() as s:
        set_manual(s, "hdfc-mid-cap", "benchmark", "NIFTY Midcap 150 TRI", "tester", NOW)
        s.commit()
    mid = parsed_pages[0]
    fields = dict(mid.fields)
    fields["expense_ratio_history"] = Fact(value="0.77%", as_on=fields["expense_ratio"].as_on)
    build_index(
        [mid.model_copy(update={"fields": fields}), *parsed_pages[1:]],
        session_factory=sf,
        embedder=FakeEmbedder(),
        headers=retrieval_headers,
        llm=FailIfCalledLLM(),
        force=True,
    )
    with sf() as s:
        row = s.get(SchemeFact, ("hdfc-mid-cap", "benchmark"))
    assert row is not None and row.value_text == "NIFTY Midcap 150 TRI"
    assert row.extracted_by == "manual"


def test_manual_value_must_pass_checks(sf: sessionmaker[Session]) -> None:
    with sf() as s, pytest.raises(ReviewError, match="rejected"):
        set_manual(s, "hdfc-mid-cap", "expense_ratio_direct", "7%", "tester", NOW)


def test_reviewer_required(sf: sessionmaker[Session]) -> None:
    with sf() as s, pytest.raises(ReviewError):
        verify_fact(s, "hdfc-mid-cap", "benchmark", "  ", NOW)


def test_unknown_fact(sf: sessionmaker[Session]) -> None:
    with sf() as s, pytest.raises(ReviewError):
        verify_fact(s, "hdfc-mid-cap", "nav", "tester", NOW)


def test_review_loop(sf: sessionmaker[Session]) -> None:
    # Row 2 is the base TER (a percentage): "9%" fails the 0-3% check and is re-asked.
    answers = iter(["x", "v", "m", "9%", "m", "0.61%", "s", "q"])
    out: list[str] = []
    with sf() as s:
        rows = list_facts(s, scheme="hdfc-mid-cap")
        confirmed, corrected = review_loop(
            s, rows, "tester", ask=lambda _: next(answers), say=out.append
        )
        keys = [r.fact_key for r in rows]
        second = s.get(SchemeFact, ("hdfc-mid-cap", keys[1]))
    assert (confirmed, corrected) == (1, 1)
    assert any("rejected" in line for line in out)  # "9%" fails the check, re-asked
    assert keys[1] == "expense_ratio_base"
    assert second is not None and second.extracted_by == "manual"
    assert second.value_text == "0.61%"
