"""EV-1-07, EV-1-12: ingest run (hash skip, partial failure, raw copies, registry)."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from sqlalchemy.orm import Session, sessionmaker

from growwbuddy.ingest.coverage import format_report, load_pages
from growwbuddy.ingest.fetch import Fetcher
from growwbuddy.ingest.manifest import Manifest
from growwbuddy.ingest.pipeline import run_ingest, save_raw
from growwbuddy.store.db import init_db, make_engine, make_session_factory
from growwbuddy.store.models import SourceState
from tests.conftest import FIXED_NOW, fixture_html, fund_json, page_with_fund

SLUG_TO_ID = {
    "hdfc-mid-cap-fund-direct-growth": "groww-hdfc-mid-cap",
    "hdfc-small-cap-fund-direct-growth": "groww-hdfc-small-cap",
    "hdfc-large-cap-fund-direct-growth": "groww-hdfc-large-cap",
    "hdfc-silver-etf-fof-direct-growth": "groww-hdfc-silver-etf-fof",
    "hdfc-multi-cap-fund-direct-growth": "groww-hdfc-multi-cap",
}


class FakeGroww:
    """Mock transport serving the saved pages; individual pages can be overridden."""

    def __init__(self) -> None:
        self.overrides: dict[str, httpx.Response | str] = {}

    def __call__(self, req: httpx.Request) -> httpx.Response:
        source_id = SLUG_TO_ID[req.url.path.rsplit("/", 1)[-1]]
        override = self.overrides.get(source_id)
        if isinstance(override, httpx.Response):
            return override
        return httpx.Response(200, text=override or fixture_html(source_id))


@pytest.fixture
def env(tmp_path: Path) -> tuple[sessionmaker[Session], Path, Path]:
    engine = make_engine(f"sqlite:///{(tmp_path / 'db' / 'test.db').as_posix()}")
    init_db(engine)
    return make_session_factory(engine), tmp_path / "raw", tmp_path / "parsed"


def run(
    manifest: Manifest,
    env: tuple[sessionmaker[Session], Path, Path],
    fake: FakeGroww,
    **kwargs: object,
) -> dict[str, str]:
    sf, raw, parsed = env
    fetcher = Fetcher(
        httpx.Client(transport=httpx.MockTransport(fake)),
        sleep=lambda _: None,
        clock=lambda: FIXED_NOW,
        max_attempts=1,
    )
    outcomes = run_ingest(
        manifest,
        fetcher=fetcher,
        session_factory=sf,
        raw_dir=raw,
        parsed_dir=parsed,
        sleep=lambda _: None,
        **kwargs,  # type: ignore[arg-type]
    )
    return {o.source_id: o.status for o in outcomes}


def test_EV_1_13_all_five_parse(
    manifest: Manifest, env: tuple[sessionmaker[Session], Path, Path]
) -> None:
    statuses = run(manifest, env, FakeGroww())
    assert set(statuses.values()) == {"parsed"}
    sf, raw, parsed = env
    assert len(list(parsed.glob("*.json"))) == 5
    assert len(list(raw.glob("*/2026-11-12.html"))) == 5
    with sf() as s:
        rows = s.query(SourceState).all()
        assert {r.status for r in rows} == {"ok"}
        assert all(r.sha256 and r.fetched_at for r in rows)


def test_EV_1_07_unchanged_content_skips_parse(
    manifest: Manifest, env: tuple[sessionmaker[Session], Path, Path]
) -> None:
    fake = FakeGroww()
    run(manifest, env, fake)
    parsed = env[2] / "groww-hdfc-mid-cap.json"
    before = parsed.stat().st_mtime_ns

    # Only dropped content (returns) changes -> hash unchanged -> skip.
    fund = fund_json("groww-hdfc-mid-cap")
    fund["return_stats"][0]["return1y"] = 42.0
    fake.overrides["groww-hdfc-mid-cap"] = page_with_fund(fund)
    statuses = run(manifest, env, fake)
    assert statuses["groww-hdfc-mid-cap"] == "unchanged"
    assert parsed.stat().st_mtime_ns == before

    # --force re-parses anyway.
    assert run(manifest, env, fake, force=True)["groww-hdfc-mid-cap"] == "parsed"


def test_changed_content_reparsed(
    manifest: Manifest, env: tuple[sessionmaker[Session], Path, Path]
) -> None:
    fake = FakeGroww()
    run(manifest, env, fake)
    fund = fund_json("groww-hdfc-mid-cap")
    fund["expense_ratio"] = "0.77"
    fake.overrides["groww-hdfc-mid-cap"] = page_with_fund(fund)
    assert run(manifest, env, fake)["groww-hdfc-mid-cap"] == "parsed"
    text = (env[2] / "groww-hdfc-mid-cap.json").read_text(encoding="utf-8")
    assert "0.77%" in text


def test_EV_1_12_partial_failure_keeps_previous(
    manifest: Manifest, env: tuple[sessionmaker[Session], Path, Path]
) -> None:
    fake = FakeGroww()
    run(manifest, env, fake)
    parsed = env[2] / "groww-hdfc-large-cap.json"
    before = parsed.read_text(encoding="utf-8")

    fake.overrides["groww-hdfc-large-cap"] = httpx.Response(503)
    fake.overrides["groww-hdfc-small-cap"] = "<html>Access denied</html>"  # bot-check
    statuses = run(manifest, env, fake)

    assert statuses["groww-hdfc-large-cap"] == "failed"
    assert statuses["groww-hdfc-small-cap"] == "failed"
    assert [s for s in statuses.values() if s != "failed"] == ["unchanged"] * 3
    assert parsed.read_text(encoding="utf-8") == before
    with env[0]() as s:
        row = s.get(SourceState, "groww-hdfc-large-cap")
        assert row is not None
        assert row.status == "failed"
        assert row.last_error and "503" in row.last_error
        assert row.sha256  # previous good hash kept


def test_only_selected_source(
    manifest: Manifest, env: tuple[sessionmaker[Session], Path, Path]
) -> None:
    statuses = run(manifest, env, FakeGroww(), only=["groww-hdfc-multi-cap"])
    assert statuses == {"groww-hdfc-multi-cap": "parsed"}


def test_raw_copies_pruned(tmp_path: Path) -> None:
    for i in range(7):
        day = (FIXED_NOW + timedelta(days=i)).date().isoformat()
        save_raw(tmp_path, "src", "<html/>", day, keep=5)
    assert len(list((tmp_path / "src").glob("*.html"))) == 5


def test_EV_1_16_coverage_report(
    manifest: Manifest, env: tuple[sessionmaker[Session], Path, Path]
) -> None:
    run(manifest, env, FakeGroww())
    report = format_report(load_pages(env[2]))
    assert "*Expense ratio" in report
    assert "ELSS lock-in / hdfc-mid-cap: not an ELSS scheme" in report
    assert "Statement / capital gains download / hdfc-mid-cap: not on the scheme page" in report
    assert "hdfc-silver-etf-fof: as of 2026-11-11 (page)" in report
