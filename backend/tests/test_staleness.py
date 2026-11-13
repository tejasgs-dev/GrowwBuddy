"""Staleness marking (task 6.6; EV-6-05, EC-RET-08, EC-VAL-09)."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.orm import Session, sessionmaker

from growwbuddy.constants import SOURCES, STALE_NOTE
from growwbuddy.pipeline.orchestrator import Pipeline
from growwbuddy.pipeline.validator import check_response, split_sentences
from growwbuddy.retrieval.headers import RetrievalHeaders
from growwbuddy.retrieval.retriever import Retriever
from growwbuddy.retrieval.vector import VectorIndex
from growwbuddy.store.models import SourceState
from tests.fakes import FailIfCalledLLM, FakeEmbedder, FakeLLM, answer_says, router_says

NOW = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)
MID_URL = next(u for _, k, u in SOURCES if k == "hdfc-mid-cap")


def set_fetched(sf: sessionmaker[Session], days_ago: float) -> None:
    with sf.begin() as s:
        for sid, _, _ in SOURCES:
            state = s.get(SourceState, sid) or SourceState(source_id=sid)
            state.fetched_at = NOW - timedelta(days=days_ago)
            state.status = "ok"
            s.add(state)


def pipeline(sf: sessionmaker[Session], llm: object, h: RetrievalHeaders) -> Pipeline:
    emb = FakeEmbedder()
    r = Retriever(VectorIndex(sf, emb.model_name), emb, sf)
    return Pipeline(llm, r, headers=h, fetched_at=r.fetched_at, stale_days=45,  # type: ignore[arg-type]
                    clock=lambda: NOW)  # fmt: skip


async def stale_answer(
    sf: sessionmaker[Session], h: RetrievalHeaders, days_ago: float
) -> tuple[str | None, str]:
    set_fetched(sf, days_ago)
    llm = FakeLLM(
        router_says("hdfc-mid-cap", query="registrar of HDFC Mid Cap Fund"),
        answer_says(
            "The registrar of HDFC Mid Cap Fund Direct Growth is CAMS.",
            cite="groww-hdfc-mid-cap#registrar",
        ),
    )
    resp = await pipeline(sf, llm, h).handle_message("Who is the registrar of HDFC Mid Cap?")
    assert resp.type == "answer"
    assert not check_response(resp)
    return resp.footer, resp.text


async def test_EV_6_05_stale_source_marks_footer_and_alerts(
    indexed: sessionmaker[Session],
    retrieval_headers: RetrievalHeaders,
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING):
        footer, text = await stale_answer(indexed, retrieval_headers, 46)
    assert footer is not None and footer.endswith(STALE_NOTE)
    assert footer.startswith("Last updated from sources: ")
    assert STALE_NOTE not in text  # outside the body (EC-VAL-09)
    assert len(split_sentences(text)) == 1
    assert any("stale source alert" in r.getMessage() for r in caplog.records)


async def test_fresh_source_has_no_note(
    indexed: sessionmaker[Session], retrieval_headers: RetrievalHeaders
) -> None:
    footer, _ = await stale_answer(indexed, retrieval_headers, 44)
    assert footer is not None and STALE_NOTE not in footer


async def test_template_replies_are_marked_too(
    indexed: sessionmaker[Session], retrieval_headers: RetrievalHeaders
) -> None:
    set_fetched(indexed, 60)
    pipe = pipeline(indexed, FailIfCalledLLM(), retrieval_headers)
    resp = await pipe.handle_message("Should I invest in HDFC Mid Cap?")
    assert resp.type == "refusal" and resp.citation is not None
    assert resp.citation.url == MID_URL
    assert resp.footer is not None and resp.footer.endswith(STALE_NOTE)


async def test_unknown_fetch_date_and_linkless_replies_unmarked(
    indexed: sessionmaker[Session], retrieval_headers: RetrievalHeaders
) -> None:
    pipe = pipeline(indexed, FailIfCalledLLM(), retrieval_headers)
    resp = await pipe.handle_message("Should I invest in HDFC Mid Cap?")  # no fetch recorded
    assert resp.footer is not None and STALE_NOTE not in resp.footer
    set_fetched(indexed, 60)
    pii = await pipe.handle_message("My PAN is ABCDE1234F")
    assert pii.footer is None
