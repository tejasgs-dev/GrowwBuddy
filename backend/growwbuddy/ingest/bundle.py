"""Daily index bundle (Phase 7, tasks 7.1, 7.3, 7.4, 7.8).

    python -m growwbuddy.ingest --bundle OUT_DIR [--previous PREV_DIR] [--force]

Runs the whole pipeline (fetch -> parse -> clean -> chunk -> embed -> facts) into a **new**
SQLite file, `OUT_DIR/bundle.sqlite`, that holds only the index tables (`sources_state`,
`chunks`, `index_meta`, `scheme_facts`, `scheme_facts_history`): never threads or messages.
The app pulls the file and merges it in (growwbuddy/refresh.py).

`PREV_DIR` holds the previous release's `manifest.json` and `bundle.sqlite`. The new build
starts from a copy of the previous bundle, so the fact diff and `scheme_facts_history` carry
on from it. When every source's content hash, the retrieval headers, the embedding model
and the bundle schema are unchanged, nothing is re-embedded and no bundle is written: only
a **heartbeat** manifest (the previous one with a new `checked_at`), which the workflow
uploads over the latest release's manifest (task 7.3).

Outputs in OUT_DIR:
- `bundle.sqlite` (bundle only) and `manifest.json` (bundle and heartbeat): the release
  assets. The manifest holds hashes, dates, counts and fact labels; no page text.
- `result.json` and `summary.md`: for the job summary and the alert issues (task 7.5).
  Never uploaded as release assets.
- `work/`: raw HTML and parsed pages. Never uploaded.

The link check (task 6.8) runs on the pages this run already fetched, so the job makes
5 Groww requests a day, not 10.
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
import sqlite3
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import Table, select
from sqlalchemy.orm import Session, sessionmaker

from growwbuddy.ingest.coverage import load_pages
from growwbuddy.ingest.fetch import Fetcher, FetchError, FetchResult
from growwbuddy.ingest.gates import (
    EXPECTED_CHUNKS,
    GateResult,
    gate_chunk_count,
    gate_core_facts,
    gate_embeddings,
    gate_linkcheck,
    gate_smoke,
    gate_sources,
    needs_review,
)
from growwbuddy.ingest.index import IndexReport, build_index
from growwbuddy.ingest.linkcheck import LinkResult, run_linkcheck
from growwbuddy.ingest.manifest import Manifest, SourceEntry
from growwbuddy.ingest.pipeline import SourceOutcome, run_ingest
from growwbuddy.llm.client import StructuredLLM
from growwbuddy.retrieval.embed import Embedder
from growwbuddy.retrieval.headers import RetrievalHeaders
from growwbuddy.store.db import make_engine, make_session_factory
from growwbuddy.store.models import (
    Base,
    ChunkRow,
    IndexMeta,
    SchemeFact,
    SchemeFactHistory,
    SourceState,
)

log = logging.getLogger(__name__)

BUNDLE_SCHEMA_VERSION = 1
BUNDLE_FILE = "bundle.sqlite"
MANIFEST_FILE = "manifest.json"
RESULT_FILE = "result.json"
SUMMARY_FILE = "summary.md"

BUNDLE_MODELS = (SourceState, ChunkRow, IndexMeta, SchemeFact, SchemeFactHistory)
BUNDLE_TABLE_NAMES = tuple(str(m.__tablename__) for m in BUNDLE_MODELS)

Outcome = Literal["bundle", "heartbeat", "failed"]


class BundleError(RuntimeError):
    """A bundle or manifest is unreadable, corrupt or incompatible."""


# ---------------------------------------------------------------- manifest


class SourceStamp(BaseModel):
    source_id: str
    scheme: str
    content_sha256: str
    as_of_date: date | None
    fetched_at: datetime  # last successful fetch; moved forward by heartbeats


class FactDiff(BaseModel):
    """Fact labels (`scheme/fact_key`) that changed against the previous bundle."""

    new: list[str] = Field(default_factory=list)
    changed: list[str] = Field(default_factory=list)
    kept_missing: list[str] = Field(default_factory=list)


class BundleManifest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    schema_version: int
    build_key: str
    headers_sha256: str
    embed_model: str
    dim: int
    chunk_count: int
    fact_count: int
    built_at: datetime
    checked_at: datetime  # heartbeat: when the daily job last confirmed the data
    sources: list[SourceStamp]
    fact_diff: FactDiff
    bundle_sha256: str
    bundle_bytes: int
    git_sha: str | None = None
    run_url: str | None = None  # the run that built the bundle
    checked_run_url: str | None = None  # the run behind `checked_at`


def read_manifest(path: Path) -> BundleManifest:
    try:
        return BundleManifest.model_validate_json(path.read_bytes())
    except FileNotFoundError as e:
        raise BundleError(f"manifest not found: {path}") from e
    except ValidationError as e:
        raise BundleError(f"manifest is invalid: {e.error_count()} errors") from e


def write_json_atomic(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 16), b""):
            h.update(block)
    return h.hexdigest()


def headers_sha256(h: RetrievalHeaders) -> str:
    canonical = json.dumps(h.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- fetch once, use twice


class ReplayFetcher(Fetcher):
    """Fetches each source once and replays the result (or the error) afterwards, so the
    link check reuses the ingest's downloads instead of fetching every page again."""

    def __init__(self, inner: Fetcher) -> None:  # no super().__init__: wraps `inner`
        self._inner = inner
        self._seen: dict[str, FetchResult | FetchError] = {}

    def now(self) -> datetime:
        return self._inner.now()

    def close(self) -> None:
        self._inner.close()

    def fetch(self, source: SourceEntry) -> FetchResult:
        if source.id not in self._seen:
            try:
                self._seen[source.id] = self._inner.fetch(source)
            except FetchError as e:
                self._seen[source.id] = e
        got = self._seen[source.id]
        if isinstance(got, FetchError):
            raise got
        return got


# ---------------------------------------------------------------- build


@dataclass(frozen=True)
class FactChange:
    scheme: str
    fact_key: str
    old: str | None
    new: str | None


@dataclass
class BuildResult:
    outcome: Outcome
    gates: list[GateResult] = field(default_factory=list)
    sources: list[SourceOutcome] = field(default_factory=list)
    linkcheck: list[LinkResult] = field(default_factory=list)
    fact_changes: list[FactChange] = field(default_factory=list)
    fact_diff: FactDiff = field(default_factory=FactDiff)
    # Exit loads stored unverified for lack of an LLM: listed in the review issue.
    needs_review: list[str] = field(default_factory=list)
    manifest: BundleManifest | None = None
    note: str = ""

    @property
    def failed_gates(self) -> list[GateResult]:
        return [g for g in self.gates if not g.ok]

    def to_json(self) -> str:
        data = {
            "outcome": self.outcome,
            "note": self.note,
            "gates": [asdict(g) for g in self.gates],
            "sources": [asdict(o) for o in self.sources],
            "linkcheck": [
                {
                    "source_id": r.source_id,
                    "scheme": r.scheme,
                    "status": r.status,
                    "problems": r.problems,
                    "warnings": r.warnings,
                }
                for r in self.linkcheck
            ],
            "fact_changes": [asdict(c) for c in self.fact_changes],
            "fact_diff": self.fact_diff.model_dump(),
            "needs_review": self.needs_review,
            "manifest": self.manifest.model_dump(mode="json") if self.manifest else None,
        }
        return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def _bundle_tables() -> list[Table]:
    return [Base.metadata.tables[name] for name in BUNDLE_TABLE_NAMES]


def load_previous(prev_dir: Path | None) -> tuple[BundleManifest | None, Path | None]:
    """The previous manifest and a bundle file that matches it, or (None, None).

    A bundle whose sha256 doesn't match its manifest is ignored, and so is the manifest:
    the next bundle is built from scratch rather than on top of a bad file.
    """
    if prev_dir is None:
        return None, None
    try:
        manifest = read_manifest(prev_dir / MANIFEST_FILE)
    except BundleError as e:
        log.warning("no usable previous manifest (%s); building from scratch", e)
        return None, None
    bundle = prev_dir / BUNDLE_FILE
    if not bundle.is_file() or sha256_file(bundle) != manifest.bundle_sha256:
        log.warning("previous bundle missing or its sha256 differs; building from scratch")
        return None, None
    if manifest.schema_version != BUNDLE_SCHEMA_VERSION:
        log.warning("previous bundle has schema %d; building from scratch", manifest.schema_version)
        return None, None
    return manifest, bundle


def _stamps(sf: sessionmaker[Session], manifest: Manifest) -> list[SourceStamp]:
    with sf() as s:
        states = {st.source_id: st for st in s.scalars(select(SourceState))}
        as_of = {
            c.source_id: c.as_of_date
            for c in s.scalars(select(ChunkRow).order_by(ChunkRow.chunk_id))
        }
    out = []
    for src in manifest.sources:
        st = states.get(src.id)
        if st is None or st.sha256 is None or st.fetched_at is None:
            continue
        fetched = st.fetched_at if st.fetched_at.tzinfo else st.fetched_at.replace(tzinfo=UTC)
        out.append(
            SourceStamp(
                source_id=src.id,
                scheme=src.scheme,
                content_sha256=st.sha256,
                as_of_date=as_of.get(src.id),
                fetched_at=fetched,
            )
        )
    return out


def _fact_changes(sf: sessionmaker[Session], report: IndexReport) -> list[FactChange]:
    wanted = set(report.facts_changed)
    latest: dict[str, SchemeFactHistory] = {}
    with sf() as s:
        for h in s.scalars(select(SchemeFactHistory).order_by(SchemeFactHistory.id)):
            label = f"{h.scheme}/{h.fact_key}"
            if label in wanted:
                latest[label] = h
    return [
        FactChange(h.scheme, h.fact_key, h.old_value, h.new_value)
        for _, h in sorted(latest.items())
    ]


def finalise_sqlite(src: Path, dst: Path) -> None:
    """Copy a (WAL-mode) database into one self-contained file: no -wal/-shm sidecars."""
    dst.unlink(missing_ok=True)
    with sqlite3.connect(src) as a, sqlite3.connect(dst) as b:
        a.backup(b)
    conn = sqlite3.connect(dst)
    try:
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.execute("VACUUM")
    finally:
        conn.close()


def build_bundle(
    out_dir: Path,
    *,
    manifest: Manifest,
    fetcher: Fetcher,
    embedder: Embedder,
    headers: RetrievalHeaders,
    expected_embed_model: str,
    llm: StructuredLLM | None = None,
    previous_dir: Path | None = None,
    force: bool = False,
    git_sha: str | None = None,
    run_url: str | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    sleep: Callable[[float], None] = time.sleep,
) -> BuildResult:
    out_dir.mkdir(parents=True, exist_ok=True)
    for name in (BUNDLE_FILE, MANIFEST_FILE, RESULT_FILE, SUMMARY_FILE):
        (out_dir / name).unlink(missing_ok=True)
    work = out_dir / "work"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)

    prev, prev_bundle = load_previous(previous_dir)
    db_path = work / "build.sqlite"
    if prev_bundle is not None:
        shutil.copyfile(prev_bundle, db_path)
    engine = make_engine(f"sqlite:///{db_path.as_posix()}")
    try:
        Base.metadata.create_all(engine, tables=_bundle_tables())
        sf = make_session_factory(engine)
        result = _build(
            work,
            sf,
            prev=prev,
            manifest=manifest,
            fetcher=ReplayFetcher(fetcher),
            embedder=embedder,
            headers=headers,
            expected_embed_model=expected_embed_model,
            llm=llm,
            force=force,
            git_sha=git_sha,
            run_url=run_url,
            now=now,
            sleep=sleep,
        )
    finally:
        engine.dispose()

    if result.outcome == "bundle":
        bundle = out_dir / BUNDLE_FILE
        finalise_sqlite(db_path, bundle)
        assert result.manifest is not None
        result.manifest.bundle_sha256 = sha256_file(bundle)
        result.manifest.bundle_bytes = bundle.stat().st_size
    if result.manifest is not None and result.outcome != "failed":
        write_json_atomic(out_dir / MANIFEST_FILE, result.manifest.model_dump_json(indent=2) + "\n")
    from growwbuddy.ingest.notify import summary_markdown  # avoid an import cycle

    write_json_atomic(out_dir / RESULT_FILE, result.to_json())
    write_json_atomic(out_dir / SUMMARY_FILE, summary_markdown(json.loads(result.to_json())))
    return result


def _build(
    work: Path,
    sf: sessionmaker[Session],
    *,
    prev: BundleManifest | None,
    manifest: Manifest,
    fetcher: Fetcher,
    embedder: Embedder,
    headers: RetrievalHeaders,
    expected_embed_model: str,
    llm: StructuredLLM | None,
    force: bool,
    git_sha: str | None,
    run_url: str | None,
    now: Callable[[], datetime],
    sleep: Callable[[float], None],
) -> BuildResult:
    parsed_dir = work / "parsed"
    outcomes = run_ingest(
        manifest,
        fetcher=fetcher,
        session_factory=sf,
        raw_dir=work / "raw",
        parsed_dir=parsed_dir,
        raw_keep=1,
        sleep=sleep,
    )
    result = BuildResult(outcome="failed", sources=outcomes)
    result.gates.append(gate_sources(outcomes, manifest))
    if not result.gates[0].ok:
        result.linkcheck = run_linkcheck(manifest, fetcher, sf)
        result.gates.append(gate_linkcheck(result.linkcheck))
        result.note = "a source failed; nothing published"
        return result

    pages = load_pages(parsed_dir)
    stamps = _stamps(sf, manifest)
    hsha = headers_sha256(headers)
    ts = now()

    unchanged = (
        not force
        and prev is not None
        and prev.embed_model == embedder.model_name == expected_embed_model
        and prev.headers_sha256 == hsha
        and {s.source_id: s.content_sha256 for s in prev.sources}
        == {s.source_id: s.content_sha256 for s in stamps}
    )
    if unchanged:
        assert prev is not None
        result.linkcheck = run_linkcheck(manifest, fetcher, sf)
        result.gates.append(gate_linkcheck(result.linkcheck))
        if result.failed_gates:
            result.note = "link check failed; no heartbeat"
            return result
        fetched = {s.source_id: s.fetched_at for s in stamps}
        sources = []
        for st in prev.sources:
            latest = max(st.fetched_at, fetched.get(st.source_id, st.fetched_at))
            sources.append(st.model_copy(update={"fetched_at": latest}))
        result.manifest = prev.model_copy(
            update={"checked_at": ts, "checked_run_url": run_url, "sources": sources}
        )
        result.outcome = "heartbeat"
        result.note = "content unchanged; heartbeat only"
        return result

    report = build_index(
        pages,
        session_factory=sf,
        embedder=embedder,
        headers=headers,
        llm=llm,
        force=True,
        now=lambda: ts,
    )
    # Again, now that the new chunks are written: `as_of_date` comes from them (before the
    # build it is the previous bundle's date, or missing on a first build).
    stamps = _stamps(sf, manifest)
    expected_chunks = prev.chunk_count if prev is not None else EXPECTED_CHUNKS
    same_model = prev is not None and prev.embed_model == expected_embed_model
    expected_dim = prev.dim if prev is not None and same_model else None
    result.gates += [
        gate_chunk_count(sf, expected_chunks),
        gate_core_facts(sf, [s.scheme for s in manifest.sources]),
        gate_embeddings(sf, expected_embed_model, expected_dim),
        gate_smoke(sf, embedder),
    ]
    result.linkcheck = run_linkcheck(manifest, fetcher, sf)
    result.gates.append(gate_linkcheck(result.linkcheck))
    result.fact_diff = FactDiff(
        new=sorted(report.facts_new) if prev is not None else [],
        changed=sorted(report.facts_changed),
        kept_missing=sorted(report.facts_kept_missing),
    )
    result.fact_changes = _fact_changes(sf, report)
    result.needs_review = needs_review(sf)
    if result.failed_gates:
        result.note = "quality gate failed; nothing published"
        return result

    with sf() as s:
        meta = s.get(IndexMeta, 1)
        assert meta is not None
        fact_count = len(s.scalars(select(SchemeFact.fact_key)).all())
        result.manifest = BundleManifest(
            schema_version=BUNDLE_SCHEMA_VERSION,
            build_key=meta.build_key,
            headers_sha256=hsha,
            embed_model=meta.embed_model,
            dim=meta.dim,
            chunk_count=meta.chunk_count,
            fact_count=fact_count,
            built_at=ts,
            checked_at=ts,
            sources=stamps,
            fact_diff=result.fact_diff,
            bundle_sha256="",  # set once the file is final
            bundle_bytes=0,
            git_sha=git_sha,
            run_url=run_url,
            checked_run_url=run_url,
        )
    result.outcome = "bundle"
    result.note = "forced rebuild" if force else "content changed; new bundle"
    return result
