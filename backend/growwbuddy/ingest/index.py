"""Build the search index and the facts table from the parsed pages (tasks 2.5, 2.7, 2.11).

All chunks are rebuilt from every parsed page on each changed ingest (<1 s for ~60). A source
whose latest fetch failed still has its previous parsed file, so its old chunks and facts
stay in use (EC-ING-23).

Embedding runs first, outside any transaction. Then chunks, index meta and facts are
written in **one** transaction, so a query sees either the old index and facts or the new
ones, never a mix (EC-ING-22, EC-RET-07). SQLite's WAL mode lets readers keep using the
old snapshot until the commit.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import delete, select
from sqlalchemy.orm import Session, sessionmaker

from growwbuddy.ingest.chunk import Chunk, chunk_page
from growwbuddy.ingest.extract_facts import ExtractedFact, extract_facts
from growwbuddy.ingest.parse_groww import ParsedPage
from growwbuddy.llm.client import StructuredLLM
from growwbuddy.retrieval.embed import Embedder
from growwbuddy.retrieval.headers import RetrievalHeaders
from growwbuddy.retrieval.vector import to_blob
from growwbuddy.store.models import ChunkRow, IndexMeta, SchemeFact, SchemeFactHistory

log = logging.getLogger(__name__)


@dataclass
class IndexReport:
    skipped: bool = False
    chunks: int = 0
    generation: int | None = None
    facts_new: list[str] = field(default_factory=list)
    facts_changed: list[str] = field(default_factory=list)
    facts_unchanged: int = 0
    facts_kept_missing: list[str] = field(default_factory=list)  # EC-FCT-14
    unverified: list[str] = field(default_factory=list)


def build_key(pages: list[ParsedPage], h: RetrievalHeaders, embed_model: str) -> str:
    payload = {
        "pages": [p.model_dump(mode="json") for p in sorted(pages, key=lambda p: p.source_id)],
        "headers": h.model_dump(mode="json"),
        "embed_model": embed_model,
    }
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _chunk_rows(chunks: list[Chunk], embedder: Embedder) -> list[ChunkRow]:
    vecs = embedder.embed_passages([c.embed_text for c in chunks])
    if vecs.shape[0] != len(chunks):
        raise RuntimeError(f"embedder returned {vecs.shape[0]} vectors for {len(chunks)} chunks")
    dim = int(vecs.shape[1])
    return [
        ChunkRow(
            **c.model_dump(),
            embed_model=embedder.model_name,
            dim=dim,
            embedding=to_blob(vecs[i]),
        )
        for i, c in enumerate(chunks)
    ]


def _fact_label(scheme: str, key: str) -> str:
    return f"{scheme}/{key}"


def _merge_facts(
    session: Session, facts: list[ExtractedFact], schemes: set[str], now: datetime
) -> IndexReport:
    """Upsert facts; keep human review while the extracted value is unchanged."""
    report = IndexReport()
    existing = {
        (f.scheme, f.fact_key): f
        for f in session.scalars(select(SchemeFact).where(SchemeFact.scheme.in_(schemes)))
    }
    seen: set[tuple[str, str]] = set()
    for new in facts:
        key = (new.scheme, new.fact_key)
        seen.add(key)
        label = _fact_label(*key)
        old = existing.get(key)
        same = (
            old is not None
            and old.extracted_value == new.value
            and (old.extracted_by == "manual" or old.check_error == new.check_error)
        )
        if old is not None and same:
            # Same value: refresh the date and evidence, keep any review or manual fix.
            old.as_of_date = new.as_of_date
            old.evidence = new.evidence
            old.source_url = new.source_url
            old.extracted_at = now
            report.facts_unchanged += 1
            if not old.verified:
                report.unverified.append(label)
            continue
        if old is not None:
            session.add(
                SchemeFactHistory(
                    scheme=new.scheme,
                    fact_key=new.fact_key,
                    old_value=old.value_text,
                    new_value=new.value,
                    old_verified=old.verified,
                    new_verified=new.verified,
                    old_extracted_by=old.extracted_by,
                    source_id=new.source_id,
                    changed_at=now,
                )
            )
            log.warning(
                "fact changed %s: %r -> %r (verified %s -> %s)",
                label,
                old.value_text,
                new.value,
                old.verified,
                new.verified,
            )
            session.delete(old)
            session.flush()
            report.facts_changed.append(label)
        else:
            report.facts_new.append(label)
        session.add(
            SchemeFact(
                scheme=new.scheme,
                fact_key=new.fact_key,
                value_text=new.value,
                extracted_value=new.value,
                source_id=new.source_id,
                source_url=new.source_url,
                section=new.section,
                as_of_date=new.as_of_date,
                extracted_by=new.extracted_by,
                verified=new.verified,
                check_error=new.check_error,
                evidence=new.evidence,
                extracted_at=now,
            )
        )
        if not new.verified:
            report.unverified.append(label)

    for key in existing:
        if key not in seen:
            # EC-FCT-14: keep the last value; the Phase 6 staleness check retires it.
            report.facts_kept_missing.append(_fact_label(*key))
            log.warning("fact %s no longer on the page; keeping the last value", key)
    return report


def build_index(
    pages: list[ParsedPage],
    *,
    session_factory: sessionmaker[Session],
    embedder: Embedder,
    headers: RetrievalHeaders,
    llm: StructuredLLM | None = None,
    force: bool = False,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> IndexReport:
    if not pages:
        raise RuntimeError("no parsed pages; run the fetch step first")
    key = build_key(pages, headers, embedder.model_name)
    with session_factory() as session:
        meta = session.get(IndexMeta, 1)
        if not force and meta is not None and meta.build_key == key:
            return IndexReport(skipped=True, chunks=meta.chunk_count, generation=meta.generation)

    chunks = [c for p in pages for c in chunk_page(p, headers)]
    rows = _chunk_rows(chunks, embedder)  # slow part, outside the transaction
    facts = [f for p in pages for f in extract_facts(p, llm)]
    ts = now()

    with session_factory() as session, session.begin():
        session.execute(delete(ChunkRow))
        session.add_all(rows)
        report = _merge_facts(session, facts, {p.scheme_key for p in pages}, ts)
        meta = session.get(IndexMeta, 1)
        if meta is None:
            meta = IndexMeta(id=1, generation=0)
            session.add(meta)
        meta.embed_model = embedder.model_name
        meta.dim = rows[0].dim
        meta.generation = (meta.generation or 0) + 1
        meta.chunk_count = len(rows)
        meta.built_at = ts
        meta.build_key = key
        report.generation = meta.generation
    report.chunks = len(rows)
    return report
