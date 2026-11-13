from __future__ import annotations

import copy
import gzip
import json
import re
from datetime import UTC, datetime
from functools import cache
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.orm import Session, sessionmaker

from growwbuddy.constants import SOURCES
from growwbuddy.ingest.manifest import Manifest, load_manifest
from growwbuddy.ingest.parse_groww import ParsedPage, parse_page
from growwbuddy.retrieval.headers import RetrievalHeaders, load_headers
from growwbuddy.store.db import init_db, make_engine, make_session_factory

FIXTURES = Path(__file__).parent / "fixtures" / "groww"
SNAPSHOTS = Path(__file__).parent / "snapshots"
FIXED_NOW = datetime(2026, 11, 12, 10, 0, tzinfo=UTC)  # fixtures were saved on this date
SOURCE_IDS = [s[0] for s in SOURCES]

_NEXT_DATA = re.compile(r'(<script[^>]*id="__NEXT_DATA__"[^>]*>)(.*?)(</script>)', re.DOTALL)


@cache
def fixture_html(source_id: str) -> str:
    """Saved copy of a real Groww page (gzipped to keep the repo small)."""
    return gzip.decompress((FIXTURES / f"{source_id}.html.gz").read_bytes()).decode("utf-8")


def fund_json(source_id: str) -> dict[str, Any]:
    m = _NEXT_DATA.search(fixture_html(source_id))
    assert m
    data: dict[str, Any] = json.loads(m.group(2))
    return copy.deepcopy(data["props"]["pageProps"]["mfServerSideData"])


def page_with_fund(fund: dict[str, Any]) -> str:
    """Minimal HTML page embedding `fund` the way Groww does."""
    blob = json.dumps({"props": {"pageProps": {"mfServerSideData": fund}}})
    return (
        '<html><body><div id="__next"></div>'
        f'<script id="__NEXT_DATA__" type="application/json">{blob}</script>'
        "</body></html>"
    )


@pytest.fixture(scope="session")
def manifest() -> Manifest:
    return load_manifest()


# ---------------------------------------------------------------- Phase 2 fixtures


@pytest.fixture(scope="session")
def parsed_pages(manifest: Manifest) -> list[ParsedPage]:
    """The five saved pages, parsed."""
    return [parse_page(fixture_html(sid), manifest.get(sid), FIXED_NOW) for sid in SOURCE_IDS]


@pytest.fixture
def session_factory(tmp_path: Path) -> sessionmaker[Session]:
    engine = make_engine(f"sqlite:///{(tmp_path / 'test.db').as_posix()}")
    init_db(engine)
    return make_session_factory(engine)


@pytest.fixture(scope="session")
def retrieval_headers() -> RetrievalHeaders:
    return load_headers()


@pytest.fixture
def indexed(
    parsed_pages: list[ParsedPage],
    session_factory: sessionmaker[Session],
    retrieval_headers: RetrievalHeaders,
) -> sessionmaker[Session]:
    """A database holding the index and facts built from the five saved pages."""
    from growwbuddy.ingest.index import build_index
    from tests.fakes import FailIfCalledLLM, FakeEmbedder

    build_index(
        parsed_pages,
        session_factory=session_factory,
        embedder=FakeEmbedder(),
        headers=retrieval_headers,
        llm=FailIfCalledLLM(),
        now=lambda: FIXED_NOW,
    )
    return session_factory
