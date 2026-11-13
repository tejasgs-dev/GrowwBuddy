"""Vector index: a SQLite `chunks` table searched in memory with NumPy (task 2.5).

The corpus is ~60 chunks, so brute-force cosine similarity takes microseconds and no vector
database is needed. Vectors live in the same SQLite file as the facts, so ingest writes
both in one transaction (EC-ING-22, EC-RET-07).

`VectorIndex` holds an immutable snapshot (rows + matrix). `reload()` builds a new snapshot
from the database and swaps the reference in one assignment, so a search sees either the
old index or the new one, never a mix.

Keep this behind `search(query_vec, scheme, k)`: sqlite-vec, pgvector or Chroma can replace
it if the corpus ever grows past ~100k chunks.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

import numpy as np
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from growwbuddy.retrieval.embed import Vectors
from growwbuddy.store.models import ChunkRow, IndexMeta


class IndexNotReadyError(RuntimeError):
    """The index is empty (ingest hasn't run) - EC-RET-06."""


class MixedIndexError(RuntimeError):
    """Stored vectors come from a different (or several) embedding models - EC-ING-25."""


@dataclass(frozen=True)
class IndexedChunk:
    chunk_id: str
    source_id: str
    scheme: str
    section: str
    title: str
    doc_type: str
    source_url: str
    as_of_date: date
    fact_as_on: date | None
    fetched_at: datetime
    text: str


@dataclass(frozen=True)
class SearchHit:
    chunk: IndexedChunk
    score: float  # cosine similarity, -1..1


def to_blob(vec: Vectors) -> bytes:
    return np.asarray(vec, dtype="<f4").tobytes()


def from_blob(blob: bytes, dim: int) -> Vectors:
    v = np.frombuffer(blob, dtype="<f4")
    if v.shape != (dim,):
        raise MixedIndexError(f"stored vector has {v.size} values, expected {dim}")
    return v.astype(np.float32)


@dataclass(frozen=True)
class _Snapshot:
    generation: int
    embed_model: str
    chunks: tuple[IndexedChunk, ...]
    matrix: Vectors  # (n, dim)
    by_scheme: dict[str, np.ndarray]  # scheme -> row indices


def check_model(stored: str, expected: str) -> None:
    if stored != expected:
        raise MixedIndexError(
            f"index was built with '{stored}' but GROWWBUDDY_EMBED_MODEL is '{expected}'; "
            "run `python -m growwbuddy.ingest` to re-embed"
        )


def _load_snapshot(session: Session, expected_model: str) -> _Snapshot:
    meta = session.get(IndexMeta, 1)
    rows = session.scalars(select(ChunkRow).order_by(ChunkRow.chunk_id)).all()
    if meta is None or not rows:
        raise IndexNotReadyError("the index is empty; run `python -m growwbuddy.ingest` first")
    check_model(meta.embed_model, expected_model)
    models = {r.embed_model for r in rows}
    if models != {meta.embed_model} or {r.dim for r in rows} != {meta.dim}:
        raise MixedIndexError(f"index mixes embedding models or sizes: {sorted(models)}")

    chunks = tuple(
        IndexedChunk(
            chunk_id=r.chunk_id,
            source_id=r.source_id,
            scheme=r.scheme,
            section=r.section,
            title=r.title,
            doc_type=r.doc_type,
            source_url=r.source_url,
            as_of_date=r.as_of_date,
            fact_as_on=r.fact_as_on,
            fetched_at=r.fetched_at,
            text=r.text,
        )
        for r in rows
    )
    matrix = np.stack([from_blob(r.embedding, meta.dim) for r in rows])
    by_scheme: dict[str, list[int]] = {}
    for i, c in enumerate(chunks):
        by_scheme.setdefault(c.scheme, []).append(i)
    return _Snapshot(
        generation=meta.generation,
        embed_model=meta.embed_model,
        chunks=chunks,
        matrix=matrix,
        by_scheme={k: np.array(v, dtype=np.int64) for k, v in by_scheme.items()},
    )


class VectorIndex:
    def __init__(self, session_factory: sessionmaker[Session], embed_model: str) -> None:
        self._session_factory = session_factory
        self._embed_model = embed_model
        self._snap: _Snapshot | None = None

    @property
    def generation(self) -> int | None:
        return self._snap.generation if self._snap else None

    def __len__(self) -> int:
        return len(self._snap.chunks) if self._snap else 0

    def reload(self) -> None:
        """Build a fresh snapshot and swap it in. Raises if the stored index is unusable."""
        with self._session_factory() as session:
            snap = _load_snapshot(session, self._embed_model)
        self._snap = snap  # single reference swap

    def refresh_if_changed(self) -> bool:
        """Reload when ingest has bumped the generation. Returns True if it reloaded."""
        with self._session_factory() as session:
            meta = session.get(IndexMeta, 1)
        current = meta.generation if meta else None
        if self._snap is None or current != self._snap.generation:
            self.reload()
            return True
        return False

    def chunks(self) -> tuple[IndexedChunk, ...]:
        """Every chunk in the current snapshot (loads it on first use)."""
        if self._snap is None:
            self.reload()
        assert self._snap is not None
        return self._snap.chunks

    def search(self, query_vec: Vectors, scheme: str, k: int = 5) -> list[SearchHit]:
        """Top-k chunks of `scheme` by cosine similarity.

        A scheme is required: searching across schemes mixes up near-identical sections
        (data research: 0.83-0.96 similarity between schemes), so the caller must ask which
        scheme instead (EC-RET-03).
        """
        snap = self._snap
        if snap is None:
            self.reload()
            snap = self._snap
        assert snap is not None
        rows = snap.by_scheme.get(scheme)
        if rows is None or k <= 0:
            return []
        q = np.asarray(query_vec, dtype=np.float32).reshape(-1)
        if q.shape[0] != snap.matrix.shape[1]:
            raise MixedIndexError(
                f"query vector has {q.shape[0]} dims, index has {snap.matrix.shape[1]}"
            )
        scores = snap.matrix[rows] @ q
        order = np.argsort(-scores, kind="stable")[:k]
        return [SearchHit(snap.chunks[int(rows[i])], float(scores[i])) for i in order]
