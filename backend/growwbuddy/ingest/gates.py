"""Quality gates for a daily index bundle (task 7.4).

Each gate returns a `GateResult`. Any failed gate fails the refresh job and nothing is
published, so the app keeps yesterday's data. Gates read only the bundle database, never
the app's.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from growwbuddy.ingest.extract_facts import CORE_FACT_KEYS, NO_LLM_EXIT_LOAD
from growwbuddy.ingest.linkcheck import LinkResult
from growwbuddy.ingest.manifest import Manifest
from growwbuddy.ingest.pipeline import SourceOutcome
from growwbuddy.retrieval.embed import Embedder
from growwbuddy.retrieval.vector import (
    IndexNotReadyError,
    MixedIndexError,
    VectorIndex,
    from_blob,
)
from growwbuddy.store.models import ChunkRow, IndexMeta, SchemeFact

# Chunks today: 5 pages x 12 sections. Used when there is no previous bundle to compare with.
EXPECTED_CHUNKS = 60
CHUNK_TOLERANCE = 0.10

# eval.md §4.2: (query, scheme, expected section). Must all be in the top 3.
SMOKE_SET: tuple[tuple[str, str, str], ...] = (
    ("exit load mid cap", "hdfc-mid-cap", "exit_load"),
    ("expense ratio small cap direct", "hdfc-small-cap", "expense_ratio"),
    ("minimum SIP large cap", "hdfc-large-cap", "minimum_investment"),
    ("silver fof benchmark", "hdfc-silver-etf-fof", "benchmark"),
    ("multi cap riskometer", "hdfc-multi-cap", "riskometer"),
    ("who manages the mid cap fund", "hdfc-mid-cap", "fund_manager"),
    ("TER large cap", "hdfc-large-cap", "expense_ratio"),
)


@dataclass(frozen=True)
class GateResult:
    name: str
    ok: bool
    detail: str


def gate_sources(outcomes: Sequence[SourceOutcome], manifest: Manifest) -> GateResult:
    """All five sources fetched and parsed. A bot-check page has no fund data, so it fails
    parsing (task 1.6) and lands here."""
    failed = [f"{o.source_id} ({o.detail})" for o in outcomes if o.status == "failed"]
    seen = {o.source_id for o in outcomes if o.status != "failed"}
    missing = sorted({s.id for s in manifest.sources} - {o.source_id for o in outcomes})
    problems = failed + [f"{sid} (not fetched)" for sid in missing]
    if problems:
        return GateResult("sources", False, "not parsed: " + "; ".join(problems))
    return GateResult("sources", True, f"{len(seen)}/{len(manifest.sources)} sources parsed")


def gate_chunk_count(sf: sessionmaker[Session], expected: int) -> GateResult:
    with sf() as s:
        n = len(s.scalars(select(ChunkRow.chunk_id)).all())
    low, high = expected * (1 - CHUNK_TOLERANCE), expected * (1 + CHUNK_TOLERANCE)
    ok = low <= n <= high
    return GateResult("chunk_count", ok, f"{n} chunks (expected {expected} ±{CHUNK_TOLERANCE:.0%})")


def needs_review(sf: sessionmaker[Session]) -> list[str]:
    """Exit loads stored unverified because the rules didn't recognise the wording and no
    LLM is configured (task 7.9). They go to the review issue, not the failure issue."""
    with sf() as s:
        rows = s.scalars(
            select(SchemeFact).where(
                SchemeFact.fact_key == "exit_load", SchemeFact.check_error == NO_LLM_EXIT_LOAD
            )
        ).all()
    return sorted(f"{r.scheme}/{r.fact_key}" for r in rows)


def gate_core_facts(sf: sessionmaker[Session], schemes: Sequence[str]) -> GateResult:
    """Every core fact (both expense ratios included) present and passing its checks.

    Exception: an exit load left unverified only for lack of an LLM (`needs_review`) passes,
    so a new exit-load wording doesn't stop the daily refresh; it is listed for review.
    """
    with sf() as s:
        rows = {
            (f.scheme, f.fact_key): f
            for f in s.scalars(select(SchemeFact).where(SchemeFact.fact_key.in_(CORE_FACT_KEYS)))
        }
    problems = []
    review = 0
    for scheme in schemes:
        for key in CORE_FACT_KEYS:
            row = rows.get((scheme, key))
            if row is not None and key == "exit_load" and row.check_error == NO_LLM_EXIT_LOAD:
                review += 1
            elif row is None or row.value_text is None:
                problems.append(f"{scheme}/{key} missing")
            elif not row.verified:
                problems.append(f"{scheme}/{key} unverified ({row.check_error})")
    if problems:
        return GateResult("core_facts", False, "; ".join(problems))
    total = len(schemes) * len(CORE_FACT_KEYS)
    detail = f"{total - review} core facts verified"
    if review:
        detail += f"; {review} exit load(s) stored unverified for review (no LLM)"
    return GateResult("core_facts", True, detail)


def gate_embeddings(
    sf: sessionmaker[Session], expected_model: str, expected_dim: int | None
) -> GateResult:
    """Model and dimension match the config; no NaN or all-zero vectors."""
    with sf() as s:
        meta = s.get(IndexMeta, 1)
        rows = s.scalars(select(ChunkRow)).all()
    if meta is None or not rows:
        return GateResult("embeddings", False, "index is empty")
    problems = []
    if meta.embed_model != expected_model or {r.embed_model for r in rows} != {expected_model}:
        problems.append(f"model {meta.embed_model!r}, config says {expected_model!r}")
    if {r.dim for r in rows} != {meta.dim}:
        problems.append("chunks have different vector sizes")
    elif expected_dim is not None and meta.dim != expected_dim:
        problems.append(f"dimension {meta.dim}, expected {expected_dim}")
    bad = []
    for r in rows:
        try:
            v = from_blob(r.embedding, meta.dim)
        except MixedIndexError:
            bad.append(r.chunk_id)
            continue
        if not np.all(np.isfinite(v)) or not np.any(v):
            bad.append(r.chunk_id)
    if bad:
        problems.append(f"{len(bad)} NaN, zero or malformed vectors (e.g. {bad[0]})")
    if problems:
        return GateResult("embeddings", False, "; ".join(problems))
    return GateResult("embeddings", True, f"{len(rows)} vectors, {meta.embed_model}, {meta.dim}d")


def gate_smoke(
    sf: sessionmaker[Session],
    embedder: Embedder,
    smoke: Sequence[tuple[str, str, str]] = SMOKE_SET,
) -> GateResult:
    index = VectorIndex(sf, embedder.model_name)
    try:
        index.reload()
    except (IndexNotReadyError, MixedIndexError) as e:
        return GateResult("retrieval_smoke", False, f"index unusable: {e}")
    misses = []
    for query, scheme, section in smoke:
        hits = index.search(embedder.embed_query(query), scheme, 3)
        if section not in [h.chunk.section for h in hits]:
            misses.append(f"{query!r} (expected {section})")
    passed = len(smoke) - len(misses)
    detail = f"{passed}/{len(smoke)} in the top 3"
    if misses:
        detail += "; missed: " + ", ".join(misses)
    return GateResult("retrieval_smoke", not misses, detail)


def gate_linkcheck(results: Sequence[LinkResult]) -> GateResult:
    """Task 7.8: a hard link-check failure (page gone, moved, wrong scheme) fails the gate;
    warnings only go to the job summary."""
    failed = [f"{r.source_id}: {'; '.join(r.problems)}" for r in results if r.status == "failed"]
    warned = sum(r.status == "warning" for r in results)
    if failed:
        return GateResult("linkcheck", False, " | ".join(failed))
    return GateResult("linkcheck", True, f"{len(results)} URLs ok, {warned} with warnings")
