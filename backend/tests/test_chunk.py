"""EV-2-01, 02, 03: chunking (tasks 2.1, 2.2)."""

from __future__ import annotations

from datetime import date

import pytest

from growwbuddy.ingest.chunk import (
    MAX_CHUNK_TOKENS,
    ChunkTooLongError,
    chunk_page,
    estimate_tokens,
)
from growwbuddy.ingest.parse_groww import ParsedPage, Section
from growwbuddy.retrieval.headers import RetrievalHeaders

FUND_DETAIL_SECTIONS = {"launch_date", "category", "registrar", "stamp_duty"}


def test_EV_2_01_one_chunk_per_section(
    parsed_pages: list[ParsedPage], retrieval_headers: RetrievalHeaders
) -> None:
    for page in parsed_pages:
        chunks = chunk_page(page, retrieval_headers)
        assert [c.section for c in chunks] == [s.name for s in page.sections]
        names = {c.section for c in chunks}
        assert "fund_details" not in names
        assert names >= FUND_DETAIL_SECTIONS
        for c in chunks:
            assert estimate_tokens(c.embed_text) <= MAX_CHUNK_TOKENS
    assert sum(len(chunk_page(p, retrieval_headers)) for p in parsed_pages) == 60


def test_token_guard_rejects_huge_section(
    parsed_pages: list[ParsedPage], retrieval_headers: RetrievalHeaders
) -> None:
    page = parsed_pages[0]
    big = page.model_copy(
        update={"sections": [Section(name="tax", title="Tax", text="word " * 400)]}
    )
    with pytest.raises(ChunkTooLongError):
        chunk_page(big, retrieval_headers)


def test_EV_2_02_stable_chunk_ids(
    parsed_pages: list[ParsedPage], retrieval_headers: RetrievalHeaders
) -> None:
    page = parsed_pages[0]
    first = [c.chunk_id for c in chunk_page(page, retrieval_headers)]
    again = [c.chunk_id for c in chunk_page(page, retrieval_headers)]
    assert first == again
    assert "groww-hdfc-mid-cap#expense_ratio" in first
    assert all(cid.startswith(f"{page.source_id}#") for cid in first)


def test_EV_2_03_chunk_metadata(
    parsed_pages: list[ParsedPage], retrieval_headers: RetrievalHeaders
) -> None:
    for page in parsed_pages:
        for c in chunk_page(page, retrieval_headers):
            assert c.scheme == page.scheme_key  # never another scheme's key (EC-ING-15)
            assert c.source_id == page.source_id
            assert c.source_url == page.url
            assert c.doc_type == "scheme_page"
            assert c.as_of_date == page.as_of_date
            assert c.title and c.text and len(c.content_sha256) == 64


def test_embed_text_has_header_and_text_does_not(
    parsed_pages: list[ParsedPage], retrieval_headers: RetrievalHeaders
) -> None:
    mid = next(p for p in parsed_pages if p.scheme_key == "hdfc-mid-cap")
    chunks = {c.section: c for c in chunk_page(mid, retrieval_headers)}
    er = chunks["expense_ratio"]
    header, _, body = er.embed_text.partition("\n")
    assert header.startswith("HDFC Mid Cap Fund (mid cap, midcap")
    assert "| Expense ratio |" in header and "TER" in header
    assert body == er.text
    for c in chunks.values():
        assert "|" not in c.text  # the header never reaches the model


def test_fact_as_on_only_where_the_section_has_a_fact_date(
    parsed_pages: list[ParsedPage], retrieval_headers: RetrievalHeaders
) -> None:
    for page in parsed_pages:
        chunks = {c.section: c for c in chunk_page(page, retrieval_headers)}
        assert chunks["expense_ratio"].fact_as_on == date(2026, 11, 10)
        assert chunks["exit_load"].fact_as_on is None
        assert chunks["tax"].fact_as_on is None
