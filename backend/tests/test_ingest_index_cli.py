"""Ingest lock (EC-ING-21) and the ingest CLI's indexing step (task 2.11)."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest
from sqlalchemy import func, select

from growwbuddy.config import Settings
from growwbuddy.ingest import __main__ as ingest_cli
from growwbuddy.ingest.lock import IngestLockedError, ingest_lock
from growwbuddy.ingest.parse_groww import ParsedPage
from growwbuddy.store.db import make_engine, make_session_factory
from growwbuddy.store.models import ChunkRow, SchemeFact
from tests.fakes import FakeEmbedder


def test_second_ingest_is_refused(tmp_path: Path) -> None:
    lock = tmp_path / "ingest.lock"
    with ingest_lock(lock), pytest.raises(IngestLockedError), ingest_lock(lock):
        pass
    assert not lock.exists()
    with ingest_lock(lock):  # free again
        pass


def test_stale_lock_is_taken_over(tmp_path: Path) -> None:
    lock = tmp_path / "ingest.lock"
    lock.write_text("12345")
    old = time.time() - 3600
    os.utime(lock, (old, old))
    with ingest_lock(lock, stale_after_s=60):
        assert lock.read_text() == str(os.getpid())


def test_cli_indexes_parsed_pages_without_fetching(
    tmp_path: Path,
    parsed_pages: list[ParsedPage],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    parsed_dir = tmp_path / "data" / "parsed"
    parsed_dir.mkdir(parents=True)
    for p in parsed_pages:
        (parsed_dir / f"{p.source_id}.json").write_text(p.model_dump_json(), encoding="utf-8")
    db_url = f"sqlite:///{(tmp_path / 'data' / 'g.db').as_posix()}"
    s = Settings(_env_file=None, db_url=db_url, parsed_dir=parsed_dir)
    monkeypatch.setattr(ingest_cli, "settings", s)
    monkeypatch.setattr(ingest_cli, "make_embedder", lambda _s: FakeEmbedder())

    assert ingest_cli.main(["--no-fetch"]) == 0
    out = capsys.readouterr().out
    assert "rebuilt: 60 chunks" in out

    with make_session_factory(make_engine(db_url))() as session:
        assert session.scalar(select(func.count()).select_from(ChunkRow)) == 60
        assert session.scalar(select(func.count()).select_from(SchemeFact)) == 40

    assert ingest_cli.main(["--no-fetch"]) == 0
    assert "up to date" in capsys.readouterr().out
    assert not (tmp_path / "data" / "ingest.lock").exists()
