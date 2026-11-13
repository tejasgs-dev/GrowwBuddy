"""Retriever (task 3.8; EC-RET-02, 03, 04, 05, 06)."""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import update
from sqlalchemy.orm import Session, sessionmaker

from growwbuddy.constants import SOURCES
from growwbuddy.retrieval.retriever import Retriever, fact_keys_for
from growwbuddy.retrieval.vector import IndexNotReadyError, VectorIndex
from growwbuddy.store.models import SchemeFact
from tests.fakes import FakeEmbedder


def make_retriever(
    sf: sessionmaker[Session], min_score: float | None = None, min_gap: float | None = None
) -> Retriever:
    emb = FakeEmbedder()
    return Retriever(VectorIndex(sf, emb.model_name), emb, sf, min_score=min_score, min_gap=min_gap)


@pytest.mark.parametrize(
    ("query", "keys"),
    [
        ("What is the expense ratio of HDFC Mid Cap Fund?", ["expense_ratio_direct"]),
        ("TER of small cap", ["expense_ratio_direct"]),
        ("What is the exit load?", ["exit_load"]),
        ("minimum SIP amount", ["min_sip"]),
        ("What is the benchmark index?", ["benchmark"]),
        ("riskometer level", ["riskometer"]),
        ("Who is the fund manager?", ["fund_manager"]),
        ("What is the launch date?", []),
        (
            "what is the exit load, expense ratio and riskometer of HDFC small cap fund ?",
            ["exit_load", "expense_ratio_direct", "riskometer"],
        ),
    ],
)
def test_fact_keys_for(query: str, keys: list[str]) -> None:
    assert fact_keys_for(query) == keys


def test_verified_fact_comes_first(indexed: sessionmaker[Session]) -> None:
    r = make_retriever(indexed).retrieve("expense ratio of HDFC Mid Cap", "hdfc-mid-cap")
    first = r.items[0]
    assert first.kind == "fact"
    assert first.item_id == "fact:hdfc-mid-cap:expense_ratio_direct"
    assert first.text == (
        "Expense ratio (TER) of HDFC Mid Cap Fund Direct Growth: 0.60% excluding GST and "
        "statutory charges, and 0.74% including them, as on 10 Nov 2026."
    )
    assert first.fact_as_on == date(2026, 11, 10)
    assert first.source_url.startswith("https://groww.in/")
    assert not r.low_confidence


@pytest.mark.parametrize(
    ("unverified", "expected"),
    [
        ("expense_ratio_base", ": 0.74% including GST and statutory charges, as on"),
        ("expense_ratio_direct", ": 0.60% excluding GST and statutory charges, as on"),
    ],
)
def test_unverified_ter_figure_is_left_out(
    indexed: sessionmaker[Session], unverified: str, expected: str
) -> None:
    """Only verified figures reach the model; the one left says which figure it is."""
    with indexed() as s:
        s.execute(
            update(SchemeFact)
            .where(SchemeFact.scheme == "hdfc-mid-cap", SchemeFact.fact_key == unverified)
            .values(verified=False)
        )
        s.commit()
    r = make_retriever(indexed).retrieve("expense ratio of HDFC Mid Cap", "hdfc-mid-cap")
    first = r.items[0]
    assert first.kind == "fact" and first.item_id == "fact:hdfc-mid-cap:expense_ratio_direct"
    assert expected in first.text
    assert ("0.60%" in first.text) == (unverified != "expense_ratio_base")


TWO_FACTS = "What is the exit load and expense ratio of HDFC small cap fund?"
MERGED_ID = "fact:hdfc-small-cap:exit_load+expense_ratio_direct"


def test_two_facts_in_one_question_become_one_item(indexed: sessionmaker[Session]) -> None:
    """One citable item states both facts, so a two-part answer passes the number check."""
    r = make_retriever(indexed).retrieve(TWO_FACTS, "hdfc-small-cap")
    facts = [i for i in r.items if i.kind == "fact"]
    assert [f.item_id for f in facts] == [MERGED_ID]
    text = facts[0].text
    assert "Exit load of 1% if redeemed within 1 year" in text
    assert "0.64% excluding GST and statutory charges, and 0.79% including them" in text
    assert facts[0].source_url.endswith("hdfc-small-cap-fund-direct-growth")
    # The older of the two dates (TER 10 Nov, exit load = page date 11 Nov).
    assert facts[0].fact_as_on == date(2026, 11, 10)
    assert r.items[0] is facts[0]  # still ahead of the chunks


def test_three_facts_become_one_item(indexed: sessionmaker[Session]) -> None:
    q = "what is the exit load, expense ratio and riskometer of HDFC small cap fund ?"
    r = make_retriever(indexed).retrieve(q, "hdfc-small-cap")
    facts = [i for i in r.items if i.kind == "fact"]
    assert [f.item_id for f in facts] == [
        "fact:hdfc-small-cap:exit_load+expense_ratio_direct+riskometer"
    ]
    for part in ("1% if redeemed within 1 year", "0.64%", "0.79%", "Very High"):
        assert part in facts[0].text


def test_two_facts_with_one_unverified_stay_single(indexed: sessionmaker[Session]) -> None:
    with indexed() as s:
        s.execute(
            update(SchemeFact)
            .where(SchemeFact.scheme == "hdfc-small-cap", SchemeFact.fact_key == "exit_load")
            .values(verified=False)
        )
        s.commit()
    r = make_retriever(indexed).retrieve(TWO_FACTS, "hdfc-small-cap")
    facts = [i for i in r.items if i.kind == "fact"]
    assert [f.item_id for f in facts] == ["fact:hdfc-small-cap:expense_ratio_direct"]


def test_top_5_chunks_only_from_the_scheme(indexed: sessionmaker[Session]) -> None:
    r = make_retriever(indexed).retrieve("exit load", "hdfc-small-cap")
    chunks = [i for i in r.items if i.kind == "chunk"]
    assert len(chunks) == 5
    assert {i.scheme for i in r.items} == {"hdfc-small-cap"}  # EC-RET-02
    scores = [c.score for c in chunks]
    assert scores == sorted(scores, reverse=True)  # type: ignore[type-var]


def test_unverified_fact_is_not_used(indexed: sessionmaker[Session]) -> None:
    with indexed() as s, s.begin():
        s.execute(
            update(SchemeFact)
            .where(SchemeFact.scheme == "hdfc-mid-cap", SchemeFact.fact_key == "exit_load")
            .values(verified=False)
        )
    r = make_retriever(indexed).retrieve("exit load", "hdfc-mid-cap")
    assert all(i.kind == "chunk" for i in r.items)  # EC-RET-05


def test_low_confidence_exit(indexed: sessionmaker[Session]) -> None:
    r = make_retriever(indexed, min_score=0.999).retrieve("launch date", "hdfc-mid-cap")
    assert r.low_confidence
    # A verified fact match is never low confidence.
    r = make_retriever(indexed, min_score=0.999).retrieve("exit load", "hdfc-mid-cap")
    assert not r.low_confidence


def test_gap_cutoff(indexed: sessionmaker[Session]) -> None:
    r = make_retriever(indexed, min_gap=10.0).retrieve("launch date", "hdfc-mid-cap")
    assert r.low_confidence and r.gap is not None
    assert not make_retriever(indexed).retrieve("launch date", "hdfc-mid-cap").low_confidence


def test_page_info(indexed: sessionmaker[Session]) -> None:
    rt = make_retriever(indexed)
    for _, scheme, url in SOURCES:
        page = rt.page(scheme)
        assert page is not None and page.url == url and page.as_of_date is not None
    assert rt.newest_date() is not None
    assert rt.page("hdfc-elss") is None


def test_empty_index(session_factory: sessionmaker[Session]) -> None:
    rt = make_retriever(session_factory)
    with pytest.raises(IndexNotReadyError):
        rt.retrieve("exit load", "hdfc-mid-cap")  # EC-RET-06
    page = rt.page("hdfc-mid-cap")  # still links the page, without a date
    assert page is not None and page.as_of_date is None
    assert rt.newest_date() is None
