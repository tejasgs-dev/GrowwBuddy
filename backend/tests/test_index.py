"""EV-2-04 and the index/facts store (tasks 2.5, 2.6, 2.11; EC-ING-22, 25; EC-RET-03, 07)."""

from __future__ import annotations

import threading
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from growwbuddy.ingest.index import IndexReport, build_index
from growwbuddy.ingest.parse_groww import Fact, ParsedPage
from growwbuddy.retrieval.headers import RetrievalHeaders
from growwbuddy.retrieval.vector import (
    IndexedChunk,
    IndexNotReadyError,
    MixedIndexError,
    VectorIndex,
)
from growwbuddy.store.models import ChunkRow, SchemeFact, SchemeFactHistory
from tests.fakes import FailIfCalledLLM, FakeEmbedder

T1 = datetime(2026, 11, 12, 10, 0, tzinfo=UTC)
T2 = datetime(2026, 12, 12, 10, 0, tzinfo=UTC)


def _build(
    pages: list[ParsedPage],
    sf: sessionmaker[Session],
    h: RetrievalHeaders,
    embedder: FakeEmbedder | None = None,
    **kw: object,
) -> IndexReport:
    return build_index(
        pages,
        session_factory=sf,
        embedder=embedder or FakeEmbedder(),
        headers=h,
        llm=FailIfCalledLLM(),
        **kw,  # type: ignore[arg-type]
    )


def _chunk_ids(sf: sessionmaker[Session], source_id: str | None = None) -> set[str]:
    with sf() as s:
        q = select(ChunkRow.chunk_id)
        if source_id:
            q = q.where(ChunkRow.source_id == source_id)
        return set(s.scalars(q))


def _with_field(page: ParsedPage, name: str, value: str) -> ParsedPage:
    fields = dict(page.fields)
    fields[name] = Fact(value=value, as_on=fields[name].as_on if name in fields else None)
    sections = [
        s.model_copy(update={"text": s.text.replace(page.fields[name].value, value)})
        if name in page.fields
        else s
        for s in page.sections
    ]
    return page.model_copy(update={"fields": fields, "sections": sections})


@pytest.fixture
def built(
    parsed_pages: list[ParsedPage],
    session_factory: sessionmaker[Session],
    retrieval_headers: RetrievalHeaders,
) -> sessionmaker[Session]:
    _build(parsed_pages, session_factory, retrieval_headers, now=lambda: T1)
    return session_factory


def test_build_writes_chunks_and_facts(built: sessionmaker[Session]) -> None:
    with built() as s:
        assert s.scalar(select(func.count()).select_from(ChunkRow)) == 60
        facts = list(s.scalars(select(SchemeFact)))
    assert len(facts) == 5 * 8  # 8 fact keys per page (two TER figures, no lock-in)
    assert all(f.verified for f in facts)
    assert {f.extracted_by for f in facts} == {"rule"}


def test_EV_2_02_rebuild_keeps_chunk_ids(
    built: sessionmaker[Session],
    parsed_pages: list[ParsedPage],
    retrieval_headers: RetrievalHeaders,
) -> None:
    before = _chunk_ids(built)
    _build(parsed_pages, built, retrieval_headers, force=True)
    assert _chunk_ids(built) == before


def test_unchanged_input_skips_rebuild(
    built: sessionmaker[Session],
    parsed_pages: list[ParsedPage],
    retrieval_headers: RetrievalHeaders,
) -> None:
    emb = FakeEmbedder()
    report = _build(parsed_pages, built, retrieval_headers, embedder=emb)
    assert report.skipped
    assert emb.passage_calls == 0  # model not even used


def test_EV_2_04_reingest_replaces_not_duplicates(
    built: sessionmaker[Session],
    parsed_pages: list[ParsedPage],
    retrieval_headers: RetrievalHeaders,
) -> None:
    mid = parsed_pages[0]
    v2 = _with_field(mid, "exit_load", "Exit load of 0.5% if redeemed within 30 days.")
    pages = [v2, *parsed_pages[1:]]
    _build(pages, built, retrieval_headers)
    with built() as s:
        rows = list(s.scalars(select(ChunkRow).where(ChunkRow.source_id == mid.source_id)))
    assert len(rows) == len(mid.sections)
    exit_rows = [r for r in rows if r.section == "exit_load"]
    assert len(exit_rows) == 1 and "0.5%" in exit_rows[0].text

    index = VectorIndex(built, "fake/bow-64")
    index.reload()
    assert len(index) == 60
    hits = index.search(FakeEmbedder().embed_query("exit load"), mid.scheme_key, k=60)
    texts = [h.chunk.text for h in hits if h.chunk.section == "exit_load"]
    assert texts == [exit_rows[0].text]


def test_section_that_disappears_removes_only_its_chunk(
    built: sessionmaker[Session],
    parsed_pages: list[ParsedPage],
    retrieval_headers: RetrievalHeaders,
) -> None:
    mid = parsed_pages[0]
    before = _chunk_ids(built)
    smaller = mid.model_copy(update={"sections": [s for s in mid.sections if s.name != "tax"]})
    _build([smaller, *parsed_pages[1:]], built, retrieval_headers)
    assert _chunk_ids(built) == before - {f"{mid.source_id}#tax"}


def test_scheme_filter_never_returns_another_scheme(built: sessionmaker[Session]) -> None:
    index = VectorIndex(built, "fake/bow-64")
    emb = FakeEmbedder()
    # The query names a *different* scheme on purpose.
    q = emb.embed_query("HDFC Small Cap Fund exit load")
    for scheme in ("hdfc-mid-cap", "hdfc-large-cap", "hdfc-silver-etf-fof"):
        hits = index.search(q, scheme, k=100)
        assert hits and {h.chunk.scheme for h in hits} == {scheme}
    assert index.search(q, "hdfc-elss", k=5) == []


def test_search_ranks_by_similarity(built: sessionmaker[Session]) -> None:
    index = VectorIndex(built, "fake/bow-64")
    hits = index.search(FakeEmbedder().embed_query("riskometer risk level"), "hdfc-mid-cap", 3)
    assert hits[0].chunk.chunk_id == "groww-hdfc-mid-cap#riskometer"
    assert [h.score for h in hits] == sorted((h.score for h in hits), reverse=True)


def test_empty_index_is_not_ready(session_factory: sessionmaker[Session]) -> None:
    with pytest.raises(IndexNotReadyError):
        VectorIndex(session_factory, "fake/bow-64").reload()


def test_model_change_refuses_old_index_then_reembeds(
    built: sessionmaker[Session],
    parsed_pages: list[ParsedPage],
    retrieval_headers: RetrievalHeaders,
) -> None:
    with pytest.raises(MixedIndexError):
        VectorIndex(built, "fake/other-model").reload()

    new = FakeEmbedder(model_name="fake/other-model", dim=32)
    report = _build(parsed_pages, built, retrieval_headers, embedder=new)
    assert not report.skipped
    with built() as s:
        assert set(s.scalars(select(ChunkRow.embed_model))) == {"fake/other-model"}
    index = VectorIndex(built, "fake/other-model")
    index.reload()
    assert len(index) == 60


def test_mixed_model_rows_are_refused(built: sessionmaker[Session]) -> None:
    with built() as s, s.begin():
        row = s.scalars(select(ChunkRow)).first()
        assert row is not None
        row.embed_model = "fake/other-model"
    with pytest.raises(MixedIndexError):
        VectorIndex(built, "fake/bow-64").reload()


def test_refresh_if_changed_follows_generation(
    built: sessionmaker[Session],
    parsed_pages: list[ParsedPage],
    retrieval_headers: RetrievalHeaders,
) -> None:
    index = VectorIndex(built, "fake/bow-64")
    assert index.refresh_if_changed()
    gen = index.generation
    assert not index.refresh_if_changed()
    _build(parsed_pages, built, retrieval_headers, force=True)
    assert index.refresh_if_changed()
    assert index.generation == (gen or 0) + 1


def test_query_during_reindex_sees_old_or_new_never_a_mix(
    built: sessionmaker[Session],
    parsed_pages: list[ParsedPage],
    retrieval_headers: RetrievalHeaders,
) -> None:
    """EC-ING-22 / EC-RET-07: readers see whole snapshots while ingest rewrites the index."""
    old_texts = {c.text for c in _snapshot_texts(built)}
    v2_pages = [
        p.model_copy(
            update={"sections": [s.model_copy(update={"text": s.text + " v2"}) for s in p.sections]}
        )
        for p in parsed_pages
    ]
    new_texts = {s.text for p in v2_pages for s in p.sections}
    seen: list[set[str]] = []
    stop = threading.Event()

    def reader() -> None:
        while not stop.is_set():
            index = VectorIndex(built, "fake/bow-64")
            try:
                index.reload()
            except IndexNotReadyError:
                seen.append(set())  # an empty read would also be a failure
                continue
            seen.append({c.text for c in _snapshot_texts_from(index)})

    t = threading.Thread(target=reader)
    t.start()
    try:
        for i in range(3):
            pages = v2_pages if i % 2 == 0 else parsed_pages
            _build(pages, built, retrieval_headers, force=True)
    finally:
        stop.set()
        t.join()
    assert seen
    for snapshot in seen:
        assert snapshot in (old_texts, new_texts)


def _snapshot_texts(sf: sessionmaker[Session]) -> list[ChunkRow]:
    with sf() as s:
        return list(s.scalars(select(ChunkRow)))


def _snapshot_texts_from(index: VectorIndex) -> list[IndexedChunk]:
    snap = index._snap
    assert snap is not None
    return list(snap.chunks)


# ---------------------------------------------------------------- facts store


def test_fact_change_writes_history_and_resets_review(
    built: sessionmaker[Session],
    parsed_pages: list[ParsedPage],
    retrieval_headers: RetrievalHeaders,
) -> None:
    with built() as s, s.begin():
        row = s.get(SchemeFact, ("hdfc-mid-cap", "expense_ratio_direct"))
        assert row is not None
        row.reviewed_by, row.reviewed_at = "tester", T1

    mid = _with_field(parsed_pages[0], "expense_ratio", "0.77%")
    mid = _with_field(mid, "expense_ratio_history", "0.77%")
    report = _build([mid, *parsed_pages[1:]], built, retrieval_headers, now=lambda: T2)
    assert report.facts_changed == ["hdfc-mid-cap/expense_ratio_direct"]

    with built() as s:
        row = s.get(SchemeFact, ("hdfc-mid-cap", "expense_ratio_direct"))
        hist = list(s.scalars(select(SchemeFactHistory)))
    assert row is not None and row.value_text == "0.77%" and row.verified
    assert row.reviewed_by is None  # a new value needs a new review
    assert len(hist) == 1
    assert (hist[0].old_value, hist[0].new_value) == ("0.74%", "0.77%")


def test_unchanged_fact_keeps_review(
    built: sessionmaker[Session],
    parsed_pages: list[ParsedPage],
    retrieval_headers: RetrievalHeaders,
) -> None:
    with built() as s, s.begin():
        row = s.get(SchemeFact, ("hdfc-mid-cap", "benchmark"))
        assert row is not None
        row.reviewed_by, row.reviewed_at = "tester", T1
    _build(parsed_pages, built, retrieval_headers, force=True, now=lambda: T2)
    with built() as s:
        row = s.get(SchemeFact, ("hdfc-mid-cap", "benchmark"))
        assert row is not None and row.reviewed_by == "tester"
        assert s.scalar(select(func.count()).select_from(SchemeFactHistory)) == 0


def test_fact_missing_after_refresh_is_kept(
    built: sessionmaker[Session],
    parsed_pages: list[ParsedPage],
    retrieval_headers: RetrievalHeaders,
) -> None:
    mid = parsed_pages[0]
    fields = {k: v for k, v in mid.fields.items() if k != "benchmark"}
    report = _build(
        [mid.model_copy(update={"fields": fields}), *parsed_pages[1:]], built, retrieval_headers
    )
    assert report.facts_kept_missing == ["hdfc-mid-cap/benchmark"]
    with built() as s:
        assert s.get(SchemeFact, ("hdfc-mid-cap", "benchmark")) is not None
