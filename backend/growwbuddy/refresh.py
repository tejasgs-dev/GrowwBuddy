"""Pull the daily index bundle into the app database (Phase 7, task 7.6).

    python -m growwbuddy.refresh                 # pull the latest index-* release (make pull-index)
    python -m growwbuddy.refresh --tag index-2026-11-12   # roll back to (and pin) a release
    python -m growwbuddy.refresh --unpin         # drop the pin and pull the latest again
    python -m growwbuddy.refresh --from-dir DIR  # import a local manifest.json + bundle.sqlite

The server runs the same pull at startup and every `GROWWBUDDY_INDEX_POLL_MINUTES` when
`GROWWBUDDY_INDEX_REPO` is set (main.py).

**Checks before import** (any failure leaves the current index live): the bundle's sha256
matches the manifest, the bundle schema version is one this code reads, and the embedding
model matches `GROWWBUDDY_EMBED_MODEL` in both the manifest and the bundle.

**Import** is one transaction on the app database, under the ingest lock (so a local
`make ingest` can't run at the same time):
- `sources_state`, `chunks` and `index_meta` are replaced; the index generation goes up, so
  `VectorIndex` reloads on its next search. WAL readers keep the old snapshot until commit,
  so a query sees the old index or the new one, never a mix.
- `scheme_facts` is **merged** with the same rule as a local ingest (ingest/index.py): an
  unchanged value keeps its `reviewed_by`/`reviewed_at` and any manual fix; a changed value
  goes to `scheme_facts_history` and its review is cleared.
- `threads` and `messages` are never touched.

The bundle is read through its own connection rather than `ATTACH`ed: SQLite can't ATTACH
inside a transaction, and the ~60 rows fit in memory, so the write is still one transaction.

A release whose bundle is the one already imported only moves the heartbeat forward
(`checked_at`, and `sources_state.fetched_at` for sources whose content hash still matches),
which keeps the staleness marker off (task 7.7).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol

import httpx
from sqlalchemy import create_engine, delete, inspect, select
from sqlalchemy.orm import Session, sessionmaker

from growwbuddy.ingest.bundle import (
    BUNDLE_FILE,
    BUNDLE_SCHEMA_VERSION,
    MANIFEST_FILE,
    BundleError,
    BundleManifest,
    read_manifest,
    sha256_file,
)
from growwbuddy.ingest.extract_facts import ExtractedFact
from growwbuddy.ingest.index import IndexReport, _merge_facts
from growwbuddy.ingest.lock import IngestLockedError, ingest_lock
from growwbuddy.store.models import BundleState, ChunkRow, IndexMeta, SchemeFact, SourceState

log = logging.getLogger(__name__)

TAG_PREFIX = "index-"
GITHUB_API = "https://api.github.com"
MAX_ASSET_BYTES = 200 * 1024 * 1024
USER_AGENT = "GrowwBuddy-index-poller"

PullStatus = Literal[
    "imported", "heartbeat", "current", "pinned", "none", "locked", "rejected", "error"
]


class BundleRejectedError(BundleError):
    """The bundle failed a check; the current index stays live."""


def _utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)  # SQLite drops the zone; stored as UTC


# ---------------------------------------------------------------- releases


@dataclass(frozen=True)
class Release:
    tag: str
    published_at: str
    assets: dict[str, str]  # asset name -> download URL


class ReleaseSource(Protocol):
    def latest(self) -> Release | None: ...

    def by_tag(self, tag: str) -> Release | None: ...

    def download(self, url: str, dest: Path) -> None: ...


class GitHubReleases:
    """`index-*` releases of one GitHub repository (public, or private with a token)."""

    def __init__(
        self, repo: str, token: str | None = None, client: httpx.Client | None = None
    ) -> None:
        self.repo = repo
        headers = {"User-Agent": USER_AGENT, "X-GitHub-Api-Version": "2022-11-28"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._client = client or httpx.Client(timeout=30.0)
        self._headers = headers

    def _release(self, data: dict[str, Any]) -> Release:
        return Release(
            tag=str(data["tag_name"]),
            published_at=str(data.get("published_at") or ""),
            assets={str(a["name"]): str(a["url"]) for a in data.get("assets", [])},
        )

    def latest(self) -> Release | None:
        resp = self._client.get(
            f"{GITHUB_API}/repos/{self.repo}/releases",
            params={"per_page": 30},
            headers={**self._headers, "Accept": "application/vnd.github+json"},
        )
        resp.raise_for_status()
        releases = [
            r
            for r in resp.json()
            if str(r.get("tag_name", "")).startswith(TAG_PREFIX)
            and not r.get("draft")
            and not r.get("prerelease")
        ]
        if not releases:
            return None
        return self._release(max(releases, key=lambda r: str(r.get("published_at") or "")))

    def by_tag(self, tag: str) -> Release | None:
        resp = self._client.get(
            f"{GITHUB_API}/repos/{self.repo}/releases/tags/{tag}",
            headers={**self._headers, "Accept": "application/vnd.github+json"},
        )
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return self._release(resp.json())

    def download(self, url: str, dest: Path) -> None:
        # The API asset URL redirects to storage; httpx drops Authorization on the
        # cross-origin hop, so the token never leaves api.github.com.
        with self._client.stream(
            "GET",
            url,
            headers={**self._headers, "Accept": "application/octet-stream"},
            follow_redirects=True,
        ) as resp:
            resp.raise_for_status()
            size = 0
            with dest.open("wb") as f:
                for block in resp.iter_bytes():
                    size += len(block)
                    if size > MAX_ASSET_BYTES:
                        raise BundleRejectedError(f"asset larger than {MAX_ASSET_BYTES} bytes")
                    f.write(block)


# ---------------------------------------------------------------- verify + import


@dataclass(frozen=True)
class BundleRows:
    sources: list[SourceState]
    chunks: list[ChunkRow]
    meta: IndexMeta
    facts: list[SchemeFact]


def _detached_copy(row: Any, model: type[Any]) -> Any:
    return model(**{a.key: getattr(row, a.key) for a in inspect(model).column_attrs})


def read_bundle(path: Path) -> BundleRows:
    engine = create_engine(f"sqlite:///{path.as_posix()}")
    try:
        tables = set(inspect(engine).get_table_names())
        needed = {"sources_state", "chunks", "index_meta", "scheme_facts"}
        if not needed <= tables:
            raise BundleRejectedError(f"bundle lacks tables: {sorted(needed - tables)}")
        with Session(engine) as s:
            meta = s.get(IndexMeta, 1)
            if meta is None:
                raise BundleRejectedError("bundle has no index_meta row")
            return BundleRows(
                sources=[_detached_copy(r, SourceState) for r in s.scalars(select(SourceState))],
                chunks=[_detached_copy(r, ChunkRow) for r in s.scalars(select(ChunkRow))],
                meta=_detached_copy(meta, IndexMeta),
                facts=[_detached_copy(r, SchemeFact) for r in s.scalars(select(SchemeFact))],
            )
    except BundleError:
        raise
    except Exception as e:  # not a SQLite file, or a corrupt one
        raise BundleRejectedError(f"bundle is unreadable: {type(e).__name__}") from e
    finally:
        engine.dispose()


def verify_bundle(manifest: BundleManifest, bundle: Path, embed_model: str) -> BundleRows:
    if manifest.schema_version != BUNDLE_SCHEMA_VERSION:
        raise BundleRejectedError(
            f"bundle schema {manifest.schema_version}, this app reads {BUNDLE_SCHEMA_VERSION}"
        )
    if manifest.embed_model != embed_model:
        raise BundleRejectedError(
            f"bundle embedded with {manifest.embed_model!r}, GROWWBUDDY_EMBED_MODEL is "
            f"{embed_model!r}"
        )
    if sha256_file(bundle) != manifest.bundle_sha256:
        raise BundleRejectedError("bundle sha256 doesn't match the manifest")
    rows = read_bundle(bundle)
    if rows.meta.embed_model != embed_model or {c.embed_model for c in rows.chunks} != {
        embed_model
    }:
        raise BundleRejectedError("bundle vectors come from a different embedding model")
    if len(rows.chunks) != manifest.chunk_count or not rows.chunks:
        raise BundleRejectedError(
            f"bundle has {len(rows.chunks)} chunks, manifest says {manifest.chunk_count}"
        )
    return rows


@dataclass
class ImportReport:
    tag: str
    generation: int
    chunks: int
    facts: IndexReport = field(default_factory=IndexReport)


def _as_extracted(f: SchemeFact) -> ExtractedFact:
    return ExtractedFact(
        scheme=f.scheme,
        fact_key=f.fact_key,
        value=f.extracted_value,
        extracted_by=f.extracted_by,
        check_error=f.check_error,
        evidence=f.evidence,
        section=f.section or "",
        as_of_date=f.as_of_date,
        source_id=f.source_id,
        source_url=f.source_url,
    )


def import_bundle(
    sf: sessionmaker[Session],
    rows: BundleRows,
    manifest: BundleManifest,
    tag: str,
    *,
    pinned_tag: str | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> ImportReport:
    ts = now()
    stamps = {s.source_id: s for s in manifest.sources}
    with sf() as s, s.begin():
        s.execute(delete(SourceState))
        s.execute(delete(ChunkRow))
        for st in rows.sources:
            stamp = stamps.get(st.source_id)
            if stamp is not None and stamp.content_sha256 == st.sha256:
                if st.fetched_at is None or _utc(st.fetched_at) < stamp.fetched_at:
                    st.fetched_at = stamp.fetched_at  # moved forward by heartbeats
                st.last_checked_at = manifest.checked_at
            s.add(st)
        s.add_all(rows.chunks)

        facts = _merge_facts(
            s, [_as_extracted(f) for f in rows.facts], {f.scheme for f in rows.facts}, ts
        )

        meta = s.get(IndexMeta, 1)
        if meta is None:
            meta = IndexMeta(id=1, generation=0)
            s.add(meta)
        meta.embed_model = rows.meta.embed_model
        meta.dim = rows.meta.dim
        meta.chunk_count = len(rows.chunks)
        meta.build_key = rows.meta.build_key
        meta.built_at = rows.meta.built_at
        meta.generation = (meta.generation or 0) + 1  # VectorIndex reloads on this

        state = s.get(BundleState, 1) or BundleState(id=1)
        state.tag = tag
        state.bundle_sha256 = manifest.bundle_sha256
        state.checked_at = manifest.checked_at
        state.imported_at = ts
        state.pinned_tag = pinned_tag
        s.add(state)
        generation = meta.generation
    for label in facts.facts_changed:
        log.warning("bundle %s changed fact %s; its review was cleared", tag, label)
    return ImportReport(tag=tag, generation=generation, chunks=len(rows.chunks), facts=facts)


def apply_heartbeat(sf: sessionmaker[Session], manifest: BundleManifest) -> bool:
    """Move `checked_at` (and fetch dates of unchanged sources) forward. True if it moved."""
    with sf() as s, s.begin():
        state = s.get(BundleState, 1)
        if state is None or _utc(state.checked_at) >= manifest.checked_at:
            return False
        for stamp in manifest.sources:
            st = s.get(SourceState, stamp.source_id)
            if st is None or st.sha256 != stamp.content_sha256:
                continue  # a local ingest changed it; don't vouch for that data
            if st.fetched_at is None or _utc(st.fetched_at) < stamp.fetched_at:
                st.fetched_at = stamp.fetched_at
            st.last_checked_at = manifest.checked_at
        state.checked_at = manifest.checked_at
    return True


# ---------------------------------------------------------------- pull


@dataclass(frozen=True)
class PullResult:
    status: PullStatus
    tag: str | None = None
    detail: str = ""


def _current_state(sf: sessionmaker[Session]) -> BundleState | None:
    with sf() as s:
        return s.get(BundleState, 1)


def _set_pin(sf: sessionmaker[Session], tag: str | None) -> None:
    with sf() as s, s.begin():
        state = s.get(BundleState, 1)
        if state is not None:
            state.pinned_tag = tag


def import_from_dir(
    sf: sessionmaker[Session],
    folder: Path,
    *,
    embed_model: str,
    lock_path: Path,
    tag: str,
    pinned_tag: str | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> PullResult:
    """Verify and import `folder/manifest.json` + `folder/bundle.sqlite`."""
    try:
        manifest = read_manifest(folder / MANIFEST_FILE)
        state = _current_state(sf)
        if state is not None and state.bundle_sha256 == manifest.bundle_sha256:
            moved = apply_heartbeat(sf, manifest)
            if state.tag != tag or state.pinned_tag != pinned_tag:
                with sf() as s, s.begin():
                    st = s.get(BundleState, 1)
                    assert st is not None
                    st.tag, st.pinned_tag = tag, pinned_tag
            return PullResult("heartbeat" if moved else "current", tag)
        rows = verify_bundle(manifest, folder / BUNDLE_FILE, embed_model)
        with ingest_lock(lock_path):
            report = import_bundle(sf, rows, manifest, tag, pinned_tag=pinned_tag, now=now)
    except IngestLockedError:
        return PullResult("locked", tag, "an ingest is running; will try again")
    except BundleError as e:
        log.error("index bundle %s rejected: %s", tag, e)
        return PullResult("rejected", tag, str(e))
    log.info(
        "imported index bundle %s: %d chunks, generation %d, %d facts changed",
        tag,
        report.chunks,
        report.generation,
        len(report.facts.facts_changed),
    )
    return PullResult(
        "imported", tag, f"{report.chunks} chunks; {len(report.facts.facts_changed)} facts changed"
    )


def pull(
    source: ReleaseSource,
    sf: sessionmaker[Session],
    *,
    embed_model: str,
    lock_path: Path,
    tag: str | None = None,
    unpin: bool = False,
    work_dir: Path | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> PullResult:
    """Import the latest release (or `tag`, which pins it). Never raises for a bad bundle."""
    try:
        if unpin:
            _set_pin(sf, None)
        state = _current_state(sf)
        if tag is None and state is not None and state.pinned_tag:
            return PullResult("pinned", state.pinned_tag, "pinned; run with --unpin to resume")
        release = source.by_tag(tag) if tag else source.latest()
        if release is None:
            return PullResult("none", tag, "no index release found")
        missing = {MANIFEST_FILE, BUNDLE_FILE} - set(release.assets)
        if missing:
            raise BundleRejectedError(f"release {release.tag} lacks {sorted(missing)}")
        if work_dir is not None:
            work_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=work_dir) as tmp:
            folder = Path(tmp)
            source.download(release.assets[MANIFEST_FILE], folder / MANIFEST_FILE)
            manifest = read_manifest(folder / MANIFEST_FILE)
            already = state is not None and state.bundle_sha256 == manifest.bundle_sha256
            if not already:
                source.download(release.assets[BUNDLE_FILE], folder / BUNDLE_FILE)
            return import_from_dir(
                sf,
                folder,
                embed_model=embed_model,
                lock_path=lock_path,
                tag=release.tag,
                pinned_tag=release.tag if tag else None,
                now=now,
            )
    except BundleError as e:
        log.error("index bundle rejected: %s", e)
        return PullResult("rejected", tag, str(e))
    except httpx.HTTPError as e:
        log.warning("index poll failed error=%s", type(e).__name__)
        return PullResult("error", tag, type(e).__name__)


async def poll_loop(pull_once: Callable[[], PullResult], every_s: float) -> None:
    """Pull at startup, then every `every_s` seconds. Never dies on an error."""
    while True:
        try:
            result = await asyncio.to_thread(pull_once)
            if result.status not in ("current", "none"):
                log.info("index poll: %s %s %s", result.status, result.tag or "", result.detail)
        except Exception as e:
            log.error("index poll crashed error=%s", type(e).__name__)
        await asyncio.sleep(every_s)


def make_puller(s: Any) -> Callable[[], PullResult]:
    """The server's poll function, from settings (`s.index_repo` must be set)."""
    from growwbuddy.store.db import init_db, make_engine, make_session_factory

    engine = make_engine(s.db_url)
    init_db(engine)
    sf = make_session_factory(engine)
    token = s.index_token.get_secret_value() if s.index_token else None
    source = GitHubReleases(s.index_repo, token)

    def pull_once() -> PullResult:
        return pull(
            source,
            sf,
            embed_model=s.embed_model,
            lock_path=s.parsed_dir.parent / "ingest.lock",
            work_dir=s.parsed_dir.parent / "bundles",
        )

    return pull_once


def main(argv: list[str] | None = None) -> int:
    from growwbuddy.config import settings
    from growwbuddy.store.db import init_db, make_engine, make_session_factory

    parser = argparse.ArgumentParser(
        prog="python -m growwbuddy.refresh",
        description="Pull the daily index bundle (GitHub release index-*) into the app database.",
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--tag", help="import this release and pin it (rollback)")
    group.add_argument("--unpin", action="store_true", help="drop a pin and pull the latest")
    group.add_argument("--from-dir", type=Path, help="import manifest.json + bundle.sqlite here")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    engine = make_engine(settings.db_url)
    init_db(engine)
    sf = make_session_factory(engine)
    lock_path = settings.parsed_dir.parent / "ingest.lock"
    if args.from_dir is not None:
        result = import_from_dir(
            sf, args.from_dir, embed_model=settings.embed_model, lock_path=lock_path,
            tag=f"local:{args.from_dir.name}",
        )  # fmt: skip
    else:
        if not settings.index_repo:
            print("error: set GROWWBUDDY_INDEX_REPO (owner/name) in .env", file=sys.stderr)
            return 2
        token = settings.index_token.get_secret_value() if settings.index_token else None
        result = pull(
            GitHubReleases(settings.index_repo, token),
            sf,
            embed_model=settings.embed_model,
            lock_path=lock_path,
            tag=args.tag,
            unpin=args.unpin,
            work_dir=settings.parsed_dir.parent / "bundles",
        )
    print(f"{result.status} {result.tag or ''} {result.detail}".rstrip())
    return 0 if result.status in ("imported", "heartbeat", "current", "pinned") else 1


if __name__ == "__main__":
    raise SystemExit(main())
