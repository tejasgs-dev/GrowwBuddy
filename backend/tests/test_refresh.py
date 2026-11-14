"""App side of the daily refresh: verify, import, heartbeat, pin, poll (Phase 7, 7.6, 7.7).

Offline: bundles are built from the saved pages with the fake embedder (tests/test_bundle.py
helpers), GitHub's API is a mocked HTTP transport, and the model is scripted.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import threading
import time
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from growwbuddy import refresh
from growwbuddy.config import Settings
from growwbuddy.constants import SOURCES, STALE_NOTE
from growwbuddy.ingest.bundle import BUNDLE_FILE, MANIFEST_FILE, read_manifest, sha256_file
from growwbuddy.ingest.lock import ingest_lock
from growwbuddy.ingest.manifest import Manifest
from growwbuddy.main import create_app
from growwbuddy.pipeline.orchestrator import Pipeline
from growwbuddy.refresh import GitHubReleases, PullResult, import_from_dir, poll_loop, pull
from growwbuddy.retrieval.headers import RetrievalHeaders
from growwbuddy.retrieval.retriever import Retriever
from growwbuddy.retrieval.vector import IndexNotReadyError, VectorIndex
from growwbuddy.store.models import (
    BundleState,
    ChunkRow,
    IndexMeta,
    Message,
    SchemeFact,
    SchemeFactHistory,
    SourceState,
    Thread,
)
from tests.api_helpers import make_services
from tests.conftest import FIXED_NOW
from tests.fakes import FakeEmbedder, FakeLLM, answer_says, router_says
from tests.test_bundle import build, pages

MODEL = "fake/bow-64"
DAY1, DAY2, DAY3 = FIXED_NOW, FIXED_NOW + timedelta(days=1), FIXED_NOW + timedelta(days=2)
TAG_A, TAG_B = "index-2026-11-12", "index-2026-11-13"
MID_URL = next(u for _, k, u in SOURCES if k == "hdfc-mid-cap")


@pytest.fixture(scope="module")
def bundles(
    tmp_path_factory: pytest.TempPathFactory,
    manifest: Manifest,
    retrieval_headers: RetrievalHeaders,
) -> dict[str, Path]:
    """A: the saved pages (day 1). B: mid cap's exit load changed (day 2), built on A.
    H: a heartbeat on A (day 3, pages unchanged)."""
    root = tmp_path_factory.mktemp("bundles")

    def lower_exit_load(fund: dict[str, Any]) -> None:
        fund["exit_load"] = fund["exit_load"].replace("1%", "0.5%")

    a = build(root / "a", manifest, retrieval_headers, now=DAY1)
    b = build(root / "b", manifest, retrieval_headers, pages(groww_hdfc_mid_cap=lower_exit_load),
              previous=root / "a", now=DAY2)  # fmt: skip
    h = build(root / "h", manifest, retrieval_headers, previous=root / "a", now=DAY3)
    assert (a.outcome, b.outcome, h.outcome) == ("bundle", "bundle", "heartbeat")
    return {"a": root / "a", "b": root / "b", "h": root / "h"}


@pytest.fixture
def app_db(session_factory: sessionmaker[Session]) -> sessionmaker[Session]:
    """The app database with one user thread in it."""
    with session_factory.begin() as s:
        s.add(Thread(id="thr_1", client_id="c" * 36, title="t", created_at=DAY1, updated_at=DAY1))
        s.flush()
        s.add(Message(id="msg_1", thread_id="thr_1", role="user", content="hi", created_at=DAY1))
    return session_factory


def do_import(sf: sessionmaker[Session], folder: Path, tag: str, tmp: Path) -> PullResult:
    return import_from_dir(
        sf, folder, embed_model=MODEL, lock_path=tmp / "ingest.lock", tag=tag, now=lambda: DAY3
    )


def generation(sf: sessionmaker[Session]) -> int | None:
    with sf() as s:
        meta = s.get(IndexMeta, 1)
        return meta.generation if meta else None


def bundle_state(sf: sessionmaker[Session]) -> BundleState:
    with sf() as s:
        st = s.get(BundleState, 1)
    assert st is not None
    return st


def chunk_texts(sf: sessionmaker[Session]) -> set[str]:
    with sf() as s:
        return set(s.scalars(select(ChunkRow.text)))


# ---------------------------------------------------------------- import


def test_import_loads_the_index_and_keeps_chats(
    app_db: sessionmaker[Session], bundles: dict[str, Path], tmp_path: Path
) -> None:
    r = do_import(app_db, bundles["a"], TAG_A, tmp_path)
    assert r.status == "imported", r
    with app_db() as s:
        assert s.scalar(select(func.count()).select_from(ChunkRow)) == 60
        assert s.scalar(select(func.count()).select_from(SourceState)) == 5
        assert s.get(Thread, "thr_1") is not None
        msg = s.get(Message, "msg_1")
        assert msg is not None and msg.content == "hi"
    st = bundle_state(app_db)
    assert st.tag == TAG_A and st.pinned_tag is None
    assert st.bundle_sha256 == read_manifest(bundles["a"] / MANIFEST_FILE).bundle_sha256
    index = VectorIndex(app_db, MODEL)
    index.reload()
    assert len(index) == 60


def test_reimport_keeps_review_of_unchanged_facts_and_clears_changed(
    app_db: sessionmaker[Session], bundles: dict[str, Path], tmp_path: Path
) -> None:
    do_import(app_db, bundles["a"], TAG_A, tmp_path)
    with app_db.begin() as s:
        for key in ("exit_load", "benchmark"):
            row = s.get(SchemeFact, ("hdfc-mid-cap", key))
            assert row is not None
            row.reviewed_by, row.reviewed_at = "tester", DAY1
    gen = generation(app_db)

    r = do_import(app_db, bundles["b"], TAG_B, tmp_path)
    assert r.status == "imported" and "1 facts changed" in r.detail
    assert generation(app_db) == (gen or 0) + 1  # VectorIndex reloads on this
    with app_db() as s:
        changed = s.get(SchemeFact, ("hdfc-mid-cap", "exit_load"))
        same = s.get(SchemeFact, ("hdfc-mid-cap", "benchmark"))
        hist = list(s.scalars(select(SchemeFactHistory)))
        assert s.get(Thread, "thr_1") is not None
    assert changed is not None and changed.value_text and "0.5%" in changed.value_text
    assert changed.reviewed_by is None and changed.reviewed_at is None
    assert same is not None and same.reviewed_by == "tester"
    assert [(h.scheme, h.fact_key) for h in hist] == [("hdfc-mid-cap", "exit_load")]


def tampered(src: Path, tmp: Path, how: str) -> Path:
    folder = tmp / f"bad-{how}"
    shutil.copytree(src, folder, ignore=shutil.ignore_patterns("work"))
    if how == "sha256":
        with (folder / BUNDLE_FILE).open("ab") as f:
            f.write(b"x")
    elif how == "schema":
        data = json.loads((folder / MANIFEST_FILE).read_text(encoding="utf-8"))
        data["schema_version"] = 99
        (folder / MANIFEST_FILE).write_text(json.dumps(data), encoding="utf-8")
    elif how == "unreadable":  # a matching sha256 but not a SQLite file
        (folder / BUNDLE_FILE).write_bytes(b"not a database")
        data = json.loads((folder / MANIFEST_FILE).read_text(encoding="utf-8"))
        data["bundle_sha256"] = sha256_file(folder / BUNDLE_FILE)
        (folder / MANIFEST_FILE).write_text(json.dumps(data), encoding="utf-8")
    return folder


@pytest.mark.parametrize(
    ("how", "message"),
    [
        ("sha256", "sha256 doesn't match"),
        ("schema", "bundle schema 99"),
        ("unreadable", "unreadable"),
    ],
)
def test_bad_bundle_is_rejected_and_old_index_stays_live(
    app_db: sessionmaker[Session],
    bundles: dict[str, Path],
    tmp_path: Path,
    how: str,
    message: str,
) -> None:
    do_import(app_db, bundles["a"], TAG_A, tmp_path)
    before = (generation(app_db), chunk_texts(app_db), bundle_state(app_db).tag)
    r = do_import(app_db, tampered(bundles["b"], tmp_path, how), TAG_B, tmp_path)
    assert r.status == "rejected" and message in r.detail, r
    assert (generation(app_db), chunk_texts(app_db), bundle_state(app_db).tag) == before


def test_bundle_from_another_embed_model_is_rejected(
    app_db: sessionmaker[Session], bundles: dict[str, Path], tmp_path: Path
) -> None:
    do_import(app_db, bundles["a"], TAG_A, tmp_path)
    gen = generation(app_db)
    r = import_from_dir(app_db, bundles["b"], embed_model="intfloat/e5-small-v2",
                        lock_path=tmp_path / "ingest.lock", tag=TAG_B)  # fmt: skip
    assert r.status == "rejected" and "GROWWBUDDY_EMBED_MODEL" in r.detail
    assert generation(app_db) == gen and bundle_state(app_db).tag == TAG_A


def test_empty_app_db_rejects_bad_bundle_without_creating_an_index(
    app_db: sessionmaker[Session], bundles: dict[str, Path], tmp_path: Path
) -> None:
    r = do_import(app_db, tampered(bundles["a"], tmp_path, "sha256"), TAG_A, tmp_path)
    assert r.status == "rejected"
    with pytest.raises(IndexNotReadyError):
        VectorIndex(app_db, MODEL).reload()


def test_import_waits_for_a_running_ingest(
    app_db: sessionmaker[Session], bundles: dict[str, Path], tmp_path: Path
) -> None:
    with ingest_lock(tmp_path / "ingest.lock"):
        r = do_import(app_db, bundles["a"], TAG_A, tmp_path)
    assert r.status == "locked"
    assert generation(app_db) is None


def test_query_during_import_sees_old_or_new_never_a_mix(
    app_db: sessionmaker[Session], bundles: dict[str, Path], tmp_path: Path
) -> None:
    """EC-ING-22 / EC-RET-07 for the bundle import (the Phase 2 test, on import_bundle)."""
    do_import(app_db, bundles["a"], TAG_A, tmp_path)
    old_texts = chunk_texts(app_db)
    do_import(app_db, bundles["b"], TAG_B, tmp_path)
    new_texts = chunk_texts(app_db)
    assert old_texts != new_texts

    seen: list[set[str]] = []
    stop = threading.Event()

    def reader() -> None:
        while not stop.is_set():
            index = VectorIndex(app_db, MODEL)
            try:
                index.reload()
            except IndexNotReadyError:
                seen.append(set())  # an empty read would also be a failure
                continue
            snap = index._snap
            assert snap is not None
            seen.append({c.text for c in snap.chunks})

    t = threading.Thread(target=reader)
    t.start()
    try:
        for i in range(4):
            folder, tag = (bundles["a"], TAG_A) if i % 2 == 0 else (bundles["b"], TAG_B)
            assert do_import(app_db, folder, tag, tmp_path).status == "imported"
    finally:
        stop.set()
        t.join()
    assert seen
    for snapshot in seen:
        assert snapshot in (old_texts, new_texts)


# ---------------------------------------------------------------- heartbeat + staleness (7.7)


def test_heartbeat_moves_checked_at_and_fetch_dates(
    app_db: sessionmaker[Session], bundles: dict[str, Path], tmp_path: Path
) -> None:
    do_import(app_db, bundles["a"], TAG_A, tmp_path)
    gen = generation(app_db)
    assert not (bundles["h"] / BUNDLE_FILE).exists()  # a heartbeat is a manifest only
    r = do_import(app_db, bundles["h"], TAG_A, tmp_path)
    assert r.status == "heartbeat"
    assert generation(app_db) == gen  # nothing re-imported
    assert refresh._utc(bundle_state(app_db).checked_at) == DAY3
    with app_db() as s:
        for st in s.scalars(select(SourceState)):
            assert st.fetched_at is not None and refresh._utc(st.fetched_at) == DAY3
    assert do_import(app_db, bundles["h"], TAG_A, tmp_path).status == "current"


def test_stale_days_default_is_three() -> None:
    assert Settings(_env_file=None).stale_days == 3


async def stale_footer(sf: sessionmaker[Session], h: RetrievalHeaders, now_days: float) -> str:
    emb = FakeEmbedder()
    r = Retriever(VectorIndex(sf, emb.model_name), emb, sf)
    llm = FakeLLM(
        router_says("hdfc-mid-cap", query="registrar of HDFC Mid Cap Fund"),
        answer_says(
            "The registrar of HDFC Mid Cap Fund Direct Growth is CAMS.",
            cite="groww-hdfc-mid-cap#registrar",
        ),
    )
    p = Pipeline(llm, r, headers=h, fetched_at=r.fetched_at,
                 stale_days=Settings(_env_file=None).stale_days,
                 clock=lambda: DAY1 + timedelta(days=now_days))  # fmt: skip
    resp = await p.handle_message("Who is the registrar of HDFC Mid Cap?")
    assert resp.type == "answer" and resp.footer
    return resp.footer


async def test_staleness_is_measured_from_the_heartbeat(
    app_db: sessionmaker[Session],
    bundles: dict[str, Path],
    tmp_path: Path,
    retrieval_headers: RetrievalHeaders,
) -> None:
    h = retrieval_headers
    do_import(app_db, bundles["a"], TAG_A, tmp_path)
    # Day 1 + 4: no run has confirmed the data for more than 3 days.
    assert (await stale_footer(app_db, h, 4)).endswith(STALE_NOTE)
    # The day-3 heartbeat confirms the pages are unchanged: 2 days old, no marker.
    do_import(app_db, bundles["h"], TAG_A, tmp_path)
    assert STALE_NOTE not in await stale_footer(app_db, h, 4)
    # Still fine at the limit, marked again after it.
    assert STALE_NOTE not in await stale_footer(app_db, h, 5)
    assert (await stale_footer(app_db, h, 5.5)).endswith(STALE_NOTE)


# ---------------------------------------------------------------- GitHub releases + pull


class FakeGitHub:
    """Mocked api.github.com: releases list, by-tag lookup, asset download via a redirect."""

    def __init__(self, bundles: dict[str, Path]) -> None:
        self.files: dict[str, Path] = {}
        self.releases: list[dict[str, Any]] = [
            {"tag_name": "v1.0", "published_at": "2026-11-14T00:00:00Z", "assets": []},
            self._release(TAG_A, "2026-11-12T05:00:00Z", bundles["a"], "a"),
            {**self._release(TAG_B, "2026-11-13T09:00:00Z", bundles["a"], "d"), "draft": True},
            self._release(TAG_B, "2026-11-13T05:00:00Z", bundles["b"], "b"),
        ]
        self.requests: list[httpx.Request] = []
        self.fail_list = False

    def _release(self, tag: str, published: str, folder: Path, key: str) -> dict[str, Any]:
        assets = []
        for name in (MANIFEST_FILE, BUNDLE_FILE):
            asset_id = f"{key}-{name}"
            self.files[asset_id] = folder / name
            assets.append(
                {
                    "name": name,
                    "url": f"https://api.github.com/repos/o/r/releases/assets/{asset_id}",
                }
            )
        return {"tag_name": tag, "published_at": published, "assets": assets}

    def downloads(self, name: str) -> int:
        return sum(
            1
            for r in self.requests
            if r.url.host == "objects.example" and r.url.path.endswith(name)
        )

    def handler(self, req: httpx.Request) -> httpx.Response:
        self.requests.append(req)
        path = req.url.path
        if req.url.host == "objects.example":
            return httpx.Response(200, content=self.files[path.rsplit("/", 1)[1]].read_bytes())
        if path == "/repos/o/r/releases":
            if self.fail_list:
                return httpx.Response(500)
            return httpx.Response(200, json=self.releases)
        if path.startswith("/repos/o/r/releases/tags/"):
            tag = path.rsplit("/", 1)[1]
            rel = next(
                (r for r in self.releases if r["tag_name"] == tag and not r.get("draft")), None
            )
            return httpx.Response(200, json=rel) if rel else httpx.Response(404)
        if path.startswith("/repos/o/r/releases/assets/"):
            asset_id = path.rsplit("/", 1)[1]
            return httpx.Response(302, headers={"Location": f"https://objects.example/{asset_id}"})
        return httpx.Response(404)


@pytest.fixture
def github(bundles: dict[str, Path]) -> FakeGitHub:
    return FakeGitHub(bundles)


@pytest.fixture
def source(github: FakeGitHub) -> Iterator[GitHubReleases]:
    client = httpx.Client(transport=httpx.MockTransport(github.handler))
    yield GitHubReleases("o/r", "secret-token", client)
    client.close()


def do_pull(src: GitHubReleases, sf: sessionmaker[Session], tmp: Path, **kw: Any) -> PullResult:
    return pull(src, sf, embed_model=MODEL, lock_path=tmp / "ingest.lock",
                work_dir=tmp / "bundles", now=lambda: DAY3, **kw)  # fmt: skip


def test_pull_imports_the_newest_index_release(
    app_db: sessionmaker[Session], source: GitHubReleases, github: FakeGitHub, tmp_path: Path
) -> None:
    r = do_pull(source, app_db, tmp_path)
    assert (r.status, r.tag) == ("imported", TAG_B)  # not v1.0, not the draft
    assert bundle_state(app_db).tag == TAG_B
    # Next poll: same bundle, so only the manifest is downloaded.
    assert do_pull(source, app_db, tmp_path).status == "current"
    assert github.downloads(BUNDLE_FILE) == 1 and github.downloads(MANIFEST_FILE) == 2
    assert not any((tmp_path / "bundles").iterdir())  # downloads are cleaned up


def test_token_goes_to_the_api_only(
    app_db: sessionmaker[Session], source: GitHubReleases, github: FakeGitHub, tmp_path: Path
) -> None:
    do_pull(source, app_db, tmp_path)
    for req in github.requests:
        if req.url.host == "api.github.com":
            assert req.headers["Authorization"] == "Bearer secret-token"
        else:
            assert "Authorization" not in req.headers


def test_tag_pins_a_release_until_unpinned(
    app_db: sessionmaker[Session], source: GitHubReleases, tmp_path: Path
) -> None:
    assert do_pull(source, app_db, tmp_path).tag == TAG_B
    r = do_pull(source, app_db, tmp_path, tag=TAG_A)  # rollback
    assert (r.status, r.tag) == ("imported", TAG_A)
    assert bundle_state(app_db).pinned_tag == TAG_A
    r = do_pull(source, app_db, tmp_path)
    assert (r.status, r.tag) == ("pinned", TAG_A)
    assert bundle_state(app_db).tag == TAG_A
    r = do_pull(source, app_db, tmp_path, unpin=True)
    assert (r.status, r.tag) == ("imported", TAG_B)
    assert bundle_state(app_db).pinned_tag is None


def test_unknown_tag_and_http_errors_change_nothing(
    app_db: sessionmaker[Session], source: GitHubReleases, github: FakeGitHub, tmp_path: Path
) -> None:
    do_pull(source, app_db, tmp_path)
    gen = generation(app_db)
    assert do_pull(source, app_db, tmp_path, tag="index-1999-01-01").status == "none"
    github.fail_list = True
    r = do_pull(source, app_db, tmp_path)
    assert (r.status, r.detail) == ("error", "HTTPStatusError")
    assert generation(app_db) == gen


def test_release_without_assets_is_rejected(
    app_db: sessionmaker[Session], source: GitHubReleases, github: FakeGitHub, tmp_path: Path
) -> None:
    github.releases.append({"tag_name": "index-2026-11-14", "published_at": "2026-11-14T05:00:00Z",
                            "assets": []})  # fmt: skip
    r = do_pull(source, app_db, tmp_path)
    assert r.status == "rejected" and "lacks" in r.detail
    assert generation(app_db) is None


async def test_poll_loop_survives_errors() -> None:
    calls = 0
    done = asyncio.Event()

    def pull_once() -> PullResult:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("boom")
        if calls >= 3:
            done.set()
        return PullResult("current")

    task = asyncio.create_task(poll_loop(pull_once, 0))
    await asyncio.wait_for(done.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert calls >= 3


def test_server_starts_the_poller_only_when_a_repo_is_set(
    session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    def fake_make_puller(s: Settings) -> Any:
        def pull_once() -> PullResult:
            calls.append(str(s.index_repo))
            return PullResult("current")

        return pull_once

    monkeypatch.setattr(refresh, "make_puller", fake_make_puller)
    svc = make_services(session_factory)
    with TestClient(create_app(svc, s=Settings(_env_file=None))):
        time.sleep(0.2)
    assert calls == []

    s = Settings(_env_file=None, index_repo="o/r", index_poll_minutes=60)
    with TestClient(create_app(make_services(session_factory), s=s)):
        deadline = time.monotonic() + 5
        while not calls and time.monotonic() < deadline:
            time.sleep(0.05)
    assert calls == ["o/r"]  # once at startup; the next poll is an hour away


def test_cli_imports_from_a_folder(
    bundles: dict[str, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from growwbuddy.config import settings

    monkeypatch.setattr(settings, "db_url", f"sqlite:///{(tmp_path / 'app.db').as_posix()}")
    monkeypatch.setattr(settings, "embed_model", MODEL)
    monkeypatch.setattr(settings, "parsed_dir", tmp_path / "parsed")
    monkeypatch.setattr(settings, "index_repo", None)
    assert refresh.main(["--from-dir", str(bundles["a"])]) == 0
    assert capsys.readouterr().out.startswith("imported local:a")
    assert refresh.main([]) == 2  # no GROWWBUDDY_INDEX_REPO
