"""Daily index bundle: builder, quality gates, change detection, alert text (Phase 7, 7.10).

Offline: the five saved Groww pages behind a mocked HTTP transport, the fake embedder, and
no Groq (an LLM that fails if called, or none at all).
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import numpy as np
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from growwbuddy.ingest import __main__ as ingest_cli
from growwbuddy.ingest import notify
from growwbuddy.ingest.bundle import (
    BUNDLE_FILE,
    BUNDLE_SCHEMA_VERSION,
    BUNDLE_TABLE_NAMES,
    MANIFEST_FILE,
    RESULT_FILE,
    SUMMARY_FILE,
    BuildResult,
    build_bundle,
    read_manifest,
    sha256_file,
)
from growwbuddy.ingest.extract_facts import NO_LLM_EXIT_LOAD
from growwbuddy.ingest.fetch import Fetcher
from growwbuddy.ingest.gates import (
    gate_chunk_count,
    gate_core_facts,
    gate_embeddings,
    gate_smoke,
    needs_review,
)
from growwbuddy.ingest.manifest import Manifest
from growwbuddy.retrieval.headers import RetrievalHeaders
from growwbuddy.retrieval.vector import to_blob
from growwbuddy.store.models import ChunkRow, SchemeFact
from tests.conftest import FIXED_NOW, SOURCE_IDS, fixture_html, fund_json, page_with_fund
from tests.fakes import FailIfCalledLLM, FakeEmbedder

LATER = FIXED_NOW + timedelta(days=1)
MID = "groww-hdfc-mid-cap"
SCHEMES = ["hdfc-mid-cap", "hdfc-small-cap", "hdfc-large-cap", "hdfc-multi-cap",
           "hdfc-silver-etf-fof"]  # fmt: skip
BOT_CHECK = "<html><body><h1>Please verify you are a human</h1></body></html>"

Pages = dict[str, str]


def pages(**changes: Callable[[dict[str, Any]], None]) -> Pages:
    """The saved pages, with `changes[source_id_with_underscores](fund)` applied."""
    out = {sid: fixture_html(sid) for sid in SOURCE_IDS}
    for key, change in changes.items():
        sid = key.replace("_", "-")
        fund = fund_json(sid)
        change(fund)
        out[sid] = page_with_fund(fund)
    return out


def fetcher_for(served: Pages, manifest: Manifest, now: datetime = FIXED_NOW) -> Fetcher:
    by_url = {s.url.rstrip("/"): s.id for s in manifest.sources}

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=served[by_url[str(req.url).rstrip("/")]])

    client = httpx.Client(transport=httpx.MockTransport(handler))
    return Fetcher(client, sleep=lambda _s: None, clock=lambda: now, max_attempts=1)


def build(
    out: Path,
    manifest: Manifest,
    headers: RetrievalHeaders,
    served: Pages | None = None,
    *,
    previous: Path | None = None,
    now: datetime = FIXED_NOW,
    embedder: FakeEmbedder | None = None,
    expected_model: str = "fake/bow-64",
    llm: Any = None,
    force: bool = False,
) -> BuildResult:
    emb = embedder or FakeEmbedder()
    return build_bundle(
        out,
        manifest=manifest,
        fetcher=fetcher_for(served or pages(), manifest, now),
        embedder=emb,
        headers=headers,
        expected_embed_model=expected_model,
        llm=llm,
        previous_dir=previous,
        force=force,
        git_sha="abc123",
        run_url="https://github.com/o/r/actions/runs/1",
        now=lambda: now,
        sleep=lambda _s: None,
    )


@pytest.fixture
def first(tmp_path: Path, manifest: Manifest, retrieval_headers: RetrievalHeaders) -> Path:
    """A first bundle (no previous release), built from the saved pages."""
    result = build(tmp_path / "first", manifest, retrieval_headers, llm=FailIfCalledLLM())
    assert result.outcome == "bundle", [g for g in result.gates if not g.ok]
    return tmp_path / "first"


def tables(db: Path) -> set[str]:
    with sqlite3.connect(db) as conn:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    return {r[0] for r in rows}


def bundle_sf(db: Path) -> sessionmaker[Session]:
    from growwbuddy.store.db import make_engine, make_session_factory

    return make_session_factory(make_engine(f"sqlite:///{db.as_posix()}"))


# ---------------------------------------------------------------- 7.1 builder + manifest


def test_bundle_holds_only_the_index_tables(first: Path) -> None:
    db = first / BUNDLE_FILE
    assert tables(db) == set(BUNDLE_TABLE_NAMES)
    assert "threads" not in tables(db) and "messages" not in tables(db)
    assert not list(first.glob("bundle.sqlite-*"))  # no -wal/-shm sidecars
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT count(*) FROM chunks").fetchone()[0] == 60
        assert conn.execute("SELECT count(*) FROM sources_state").fetchone()[0] == 5
        assert conn.execute("SELECT count(*) FROM index_meta").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM scheme_facts").fetchone()[0] > 30
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"


def test_manifest_has_every_field_and_no_page_text(first: Path) -> None:
    raw = (first / MANIFEST_FILE).read_text(encoding="utf-8")
    data = json.loads(raw)
    for key in ("schema_version", "build_key", "embed_model", "dim", "chunk_count",
                "fact_count", "sources", "checked_at", "fact_diff", "git_sha", "run_url",
                "bundle_sha256"):  # fmt: skip
        assert data[key] is not None, key
    assert data["schema_version"] == BUNDLE_SCHEMA_VERSION
    assert (data["embed_model"], data["dim"], data["chunk_count"]) == ("fake/bow-64", 64, 60)
    assert data["bundle_sha256"] == sha256_file(first / BUNDLE_FILE)
    assert data["bundle_bytes"] == (first / BUNDLE_FILE).stat().st_size
    assert {s["source_id"] for s in data["sources"]} == set(SOURCE_IDS)
    for s in data["sources"]:
        assert len(s["content_sha256"]) == 64 and s["as_of_date"] and s["fetched_at"]
    assert data["git_sha"] == "abc123" and data["run_url"].endswith("/runs/1")

    with sqlite3.connect(first / BUNDLE_FILE) as conn:
        texts = [r[0] for r in conn.execute("SELECT text FROM chunks")]
    for text in texts:  # no page text in the manifest, nor in the summary or result
        snippet = text[-60:]
        assert snippet not in raw
        assert snippet not in (first / SUMMARY_FILE).read_text(encoding="utf-8")
        assert snippet not in (first / RESULT_FILE).read_text(encoding="utf-8")


# ---------------------------------------------------------------- 7.3 change detection


def test_unchanged_pages_give_a_heartbeat_not_a_bundle(
    first: Path, tmp_path: Path, manifest: Manifest, retrieval_headers: RetrievalHeaders
) -> None:
    prev = read_manifest(first / MANIFEST_FILE)
    emb = FakeEmbedder()
    out = tmp_path / "second"
    result = build(out, manifest, retrieval_headers, previous=first, now=LATER, embedder=emb)
    assert result.outcome == "heartbeat"
    assert emb.passage_calls == 0  # nothing re-embedded
    assert not (out / BUNDLE_FILE).exists()
    beat = read_manifest(out / MANIFEST_FILE)
    assert beat.bundle_sha256 == prev.bundle_sha256  # still describes the published bundle
    assert beat.built_at == prev.built_at and beat.build_key == prev.build_key
    assert beat.checked_at == LATER > prev.checked_at
    assert all(s.fetched_at == LATER for s in beat.sources)


def test_force_builds_a_bundle_even_when_unchanged(
    first: Path, tmp_path: Path, manifest: Manifest, retrieval_headers: RetrievalHeaders
) -> None:
    out = tmp_path / "forced"
    result = build(out, manifest, retrieval_headers, previous=first, now=LATER, force=True)
    assert result.outcome == "bundle" and (out / BUNDLE_FILE).exists()
    assert result.fact_changes == []


def test_changed_fact_builds_a_bundle_with_a_diff(
    first: Path, tmp_path: Path, manifest: Manifest, retrieval_headers: RetrievalHeaders
) -> None:
    def lower_exit_load(fund: dict[str, Any]) -> None:
        fund["exit_load"] = fund["exit_load"].replace("1%", "0.5%")

    out = tmp_path / "changed"
    served = pages(groww_hdfc_mid_cap=lower_exit_load)
    result = build(out, manifest, retrieval_headers, served, previous=first, now=LATER)
    assert result.outcome == "bundle", result.failed_gates
    assert result.fact_diff.changed == ["hdfc-mid-cap/exit_load"]
    [change] = result.fact_changes
    assert change.old is not None and "1%" in change.old
    assert change.new is not None and "0.5%" in change.new
    m = read_manifest(out / MANIFEST_FILE)
    assert m.fact_diff.changed == ["hdfc-mid-cap/exit_load"]
    with sqlite3.connect(out / BUNDLE_FILE) as conn:  # history carries on from the previous
        assert conn.execute("SELECT count(*) FROM scheme_facts_history").fetchone()[0] == 1


def test_previous_bundle_with_a_bad_sha256_is_ignored(
    first: Path, tmp_path: Path, manifest: Manifest, retrieval_headers: RetrievalHeaders
) -> None:
    with (first / BUNDLE_FILE).open("ab") as f:
        f.write(b"tampered")
    result = build(tmp_path / "next", manifest, retrieval_headers, previous=first, now=LATER)
    assert result.outcome == "bundle"  # rebuilt from scratch, not a heartbeat on a bad file


# ---------------------------------------------------------------- 7.4 gates fail closed


def assert_nothing_published(out: Path, result: BuildResult, gate: str) -> None:
    assert result.outcome == "failed"
    assert gate in [g.name for g in result.failed_gates], result.gates
    assert not (out / BUNDLE_FILE).exists()
    assert not (out / MANIFEST_FILE).exists()
    assert json.loads((out / RESULT_FILE).read_text(encoding="utf-8"))["outcome"] == "failed"


def test_bot_check_page_fails_the_sources_gate(
    tmp_path: Path, manifest: Manifest, retrieval_headers: RetrievalHeaders
) -> None:
    served = {**pages(), MID: BOT_CHECK}
    out = tmp_path / "out"
    result = build(out, manifest, retrieval_headers, served)
    assert_nothing_published(out, result, "sources")
    assert "linkcheck" in [g.name for g in result.failed_gates]


def test_bot_check_page_also_blocks_a_heartbeat(
    first: Path, tmp_path: Path, manifest: Manifest, retrieval_headers: RetrievalHeaders
) -> None:
    out = tmp_path / "out"
    result = build(out, manifest, retrieval_headers, {**pages(), MID: BOT_CHECK},
                   previous=first, now=LATER)  # fmt: skip
    assert_nothing_published(out, result, "sources")


def test_wrong_embed_model_fails_the_embeddings_gate(
    tmp_path: Path, manifest: Manifest, retrieval_headers: RetrievalHeaders
) -> None:
    out = tmp_path / "out"
    result = build(out, manifest, retrieval_headers, expected_model="intfloat/e5-small-v2")
    assert_nothing_published(out, result, "embeddings")


def test_missing_core_fact_fails_the_build(
    tmp_path: Path, manifest: Manifest, retrieval_headers: RetrievalHeaders
) -> None:
    def drop_ter(fund: dict[str, Any]) -> None:
        fund["expense_ratio"] = None

    out = tmp_path / "out"
    result = build(out, manifest, retrieval_headers, pages(groww_hdfc_mid_cap=drop_ter))
    assert_nothing_published(out, result, "core_facts")
    detail = next(g.detail for g in result.failed_gates if g.name == "core_facts")
    assert "hdfc-mid-cap/expense_ratio_direct" in detail


def test_unverified_core_fact_fails_the_gate(indexed: sessionmaker[Session]) -> None:
    assert gate_core_facts(indexed, SCHEMES).ok
    with indexed.begin() as s:
        row = s.get(SchemeFact, ("hdfc-small-cap", "benchmark"))
        assert row is not None
        row.verified, row.check_error = False, "sanity check failed"
    g = gate_core_facts(indexed, SCHEMES)
    assert not g.ok and "hdfc-small-cap/benchmark unverified" in g.detail


@pytest.mark.parametrize("bad", ["zero", "nan"])
def test_zero_or_nan_vector_fails_the_embeddings_gate(
    indexed: sessionmaker[Session], bad: str
) -> None:
    assert gate_embeddings(indexed, "fake/bow-64", 64).ok
    vec = np.zeros(64, dtype=np.float32) if bad == "zero" else np.full(64, np.nan, np.float32)
    with indexed.begin() as s:
        row = s.scalars(select(ChunkRow).limit(1)).one()
        row.embedding = to_blob(vec)
    g = gate_embeddings(indexed, "fake/bow-64", 64)
    assert not g.ok and "NaN, zero or malformed" in g.detail


def test_wrong_dimension_fails_the_embeddings_gate(indexed: sessionmaker[Session]) -> None:
    g = gate_embeddings(indexed, "fake/bow-64", 384)
    assert not g.ok and "dimension 64, expected 384" in g.detail


def test_smoke_miss_fails_the_gate(indexed: sessionmaker[Session]) -> None:
    emb = FakeEmbedder()
    assert gate_smoke(indexed, emb).ok  # the eval.md §4.2 set: 7/7
    g = gate_smoke(indexed, emb, [("exit load mid cap", "hdfc-mid-cap", "no_such_section")])
    assert not g.ok and "0/1 in the top 3" in g.detail


def test_chunk_count_outside_ten_percent_fails(indexed: sessionmaker[Session]) -> None:
    assert gate_chunk_count(indexed, 60).ok
    assert gate_chunk_count(indexed, 66).ok  # 60 is within 10% of 66
    assert not gate_chunk_count(indexed, 70).ok
    assert not gate_chunk_count(indexed, 50).ok


# ---------------------------------------------------------------- 7.9 no Groq key


def test_unknown_exit_load_without_llm_is_stored_unverified_and_listed(
    tmp_path: Path, manifest: Manifest, retrieval_headers: RetrievalHeaders
) -> None:
    def new_wording(fund: dict[str, Any]) -> None:
        fund["exit_load"] = "Exit load is charged as per the SID for redemptions."

    out = tmp_path / "out"
    result = build(out, manifest, retrieval_headers, pages(groww_hdfc_mid_cap=new_wording))
    assert result.outcome == "bundle", result.failed_gates  # no LLM: published, not failed
    assert result.needs_review == ["hdfc-mid-cap/exit_load"]
    core = next(g for g in result.gates if g.name == "core_facts")
    assert core.ok and "1 exit load(s) stored unverified" in core.detail

    sf = bundle_sf(out / BUNDLE_FILE)
    assert needs_review(sf) == ["hdfc-mid-cap/exit_load"]
    with sf() as s:
        row = s.get(SchemeFact, ("hdfc-mid-cap", "exit_load"))
    assert row is not None and not row.verified and row.value_text is None
    assert row.check_error == NO_LLM_EXIT_LOAD

    data = json.loads((out / RESULT_FILE).read_text(encoding="utf-8"))
    assert data["needs_review"] == ["hdfc-mid-cap/exit_load"]
    body = notify.facts_body(data, "https://run")
    assert "- hdfc-mid-cap/exit_load" in body and "Exit-load wording not recognised" in body
    assert "hdfc-mid-cap/exit_load" in (out / SUMMARY_FILE).read_text(encoding="utf-8")


def test_bundle_cli_builds_without_groq_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The workflow has no GROQ_API_KEY: the CLI passes no LLM instead of a client that
    would fail on its first call."""
    seen: dict[str, Any] = {}

    def fake_build(out_dir: Path, **kw: Any) -> BuildResult:
        seen.update(kw, out_dir=out_dir)
        return BuildResult(outcome="heartbeat", note="test")

    monkeypatch.setattr("growwbuddy.config.settings.groq_api_key", None)
    monkeypatch.setattr("growwbuddy.ingest.bundle.build_bundle", fake_build)
    monkeypatch.setattr(ingest_cli, "make_embedder", lambda _s: FakeEmbedder())
    monkeypatch.setenv("GITHUB_SERVER_URL", "https://github.com")
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setenv("GITHUB_RUN_ID", "7")
    assert ingest_cli.main(["--bundle", str(tmp_path / "out")]) == 0
    assert seen["llm"] is None
    assert seen["run_url"] == "https://github.com/o/r/actions/runs/7"


def test_bundle_cli_rejects_conflicting_options(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as e:
        ingest_cli.main(["--previous", str(tmp_path)])
    assert e.value.code == 2
    with pytest.raises(SystemExit) as e:
        ingest_cli.main(["--bundle", str(tmp_path), "--no-fetch"])
    assert e.value.code == 2


# ---------------------------------------------------------------- 7.5 alert text


def test_failure_issue_names_the_failing_gate(
    tmp_path: Path, manifest: Manifest, retrieval_headers: RetrievalHeaders
) -> None:
    out = tmp_path / "out"
    build(out, manifest, retrieval_headers, {**pages(), MID: BOT_CHECK})
    data = json.loads((out / RESULT_FILE).read_text(encoding="utf-8"))
    body = notify.failure_body(data, "https://run")
    assert "`sources`" in body and MID in body and "[this run](https://run)" in body
    assert "Please verify you are a human" not in body  # no page text


def test_failure_issue_without_result_file(capsys: pytest.CaptureFixture[str]) -> None:
    assert notify.main(["failure", "does/not/exist.json", "--run-url", "https://run"]) == 0
    assert "stopped before the quality gates ran" in capsys.readouterr().out


def test_facts_issue_lists_old_and_new_values() -> None:
    data = {
        "fact_changes": [
            {"scheme": "hdfc-mid-cap", "fact_key": "exit_load", "old": "1% | a", "new": "0.5%"}
        ],
        "fact_diff": {"kept_missing": ["hdfc-mid-cap/lock_in"]},
    }
    body = notify.facts_body(data, None)
    assert "| hdfc-mid-cap | exit_load | 1% \\| a | 0.5% |" in body  # pipes escaped
    assert "No longer on the page (last value kept): hdfc-mid-cap/lock_in" in body


def test_summary_cli_prints_gates(first: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert notify.main(["summary", str(first / RESULT_FILE)]) == 0
    out = capsys.readouterr().out
    assert out.startswith("## Daily data refresh: bundle")
    assert "| retrieval_smoke | pass | 7/7 in the top 3 |" in out


def test_result_json_timestamps_are_utc(first: Path) -> None:
    m = read_manifest(first / MANIFEST_FILE)
    assert m.checked_at == FIXED_NOW and m.checked_at.tzinfo is not None
    assert m.checked_at.astimezone(UTC) == FIXED_NOW
