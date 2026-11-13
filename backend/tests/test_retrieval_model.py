"""EV-2-11 and the retrieval-accuracy exit check, with the real e5 model.

Skipped by default (CI never downloads the model). Run locally with:

    GROWWBUDDY_RUN_MODEL_TESTS=1 python -m pytest tests/test_retrieval_model.py
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy.orm import Session, sessionmaker

from growwbuddy.ingest.index import build_index
from growwbuddy.ingest.parse_groww import ParsedPage
from growwbuddy.retrieval.debug import Searcher
from growwbuddy.retrieval.embed import E5Embedder
from growwbuddy.retrieval.evaluate import DEFAULT_FILE, load_queries, run, summarise
from growwbuddy.retrieval.headers import RetrievalHeaders
from growwbuddy.retrieval.vector import VectorIndex
from tests.fakes import FailIfCalledLLM

pytestmark = [
    pytest.mark.model,
    pytest.mark.skipif(
        os.environ.get("GROWWBUDDY_RUN_MODEL_TESTS") != "1",
        reason="needs the real embedding model; set GROWWBUDDY_RUN_MODEL_TESTS=1",
    ),
]

SMOKE = [  # docs/eval.md §4.2
    ("exit load mid cap", "hdfc-mid-cap", "exit_load"),
    ("expense ratio small cap direct", "hdfc-small-cap", "expense_ratio"),
    ("minimum SIP large cap", "hdfc-large-cap", "minimum_investment"),
    ("silver fof benchmark", "hdfc-silver-etf-fof", "benchmark"),
    ("multi cap riskometer", "hdfc-multi-cap", "riskometer"),
    ("who manages the mid cap fund", "hdfc-mid-cap", "fund_manager"),
    ("TER large cap", "hdfc-large-cap", "expense_ratio"),
]


@pytest.fixture(scope="module")
def embedder() -> E5Embedder:
    return E5Embedder("intfloat/e5-small-v2")


@pytest.fixture
def searcher(
    parsed_pages: list[ParsedPage],
    session_factory: sessionmaker[Session],
    retrieval_headers: RetrievalHeaders,
    embedder: E5Embedder,
) -> Searcher:
    build_index(
        parsed_pages,
        session_factory=session_factory,
        embedder=embedder,
        headers=retrieval_headers,
        llm=FailIfCalledLLM(),
    )
    index = VectorIndex(session_factory, embedder.model_name)
    index.reload()
    return Searcher(index, embedder)


@pytest.mark.parametrize(("query", "scheme", "section"), SMOKE)
def test_EV_2_11_smoke_top3(searcher: Searcher, query: str, scheme: str, section: str) -> None:
    hits = searcher.search(query, scheme, 3)
    assert section in [h.chunk.section for h in hits]


def test_retrieval_queries_meet_targets(searcher: Searcher) -> None:
    queries = load_queries(DEFAULT_FILE)
    assert len(queries) == 180
    s = summarise(run(queries, searcher))
    assert s["top1"] >= 0.90, s
    assert s["top3"] >= 0.97, s
