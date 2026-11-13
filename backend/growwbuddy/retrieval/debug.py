"""Search the index from the terminal (EV-2-11).

    python -m growwbuddy.retrieval.debug "exit load mid cap"
    python -m growwbuddy.retrieval.debug "who manages it" --scheme hdfc-mid-cap

The scheme comes from `--scheme` or from the scheme named in the query. As in production,
the search never runs across schemes (EC-RET-03).
"""

from __future__ import annotations

import argparse
import sys

from growwbuddy.config import Settings, settings
from growwbuddy.constants import SCHEME_KEYS
from growwbuddy.retrieval.embed import Embedder, EmbedderUnavailableError, make_embedder
from growwbuddy.retrieval.headers import resolve_scheme
from growwbuddy.retrieval.vector import (
    IndexNotReadyError,
    MixedIndexError,
    SearchHit,
    VectorIndex,
)
from growwbuddy.store.db import init_db, make_engine, make_session_factory


class Searcher:
    def __init__(self, index: VectorIndex, embedder: Embedder) -> None:
        self.index = index
        self.embedder = embedder

    def search(self, query: str, scheme: str, k: int = 5) -> list[SearchHit]:
        return self.index.search(self.embedder.embed_query(query), scheme, k)


def open_searcher(s: Settings = settings) -> Searcher:
    engine = make_engine(s.db_url)
    init_db(engine)
    index = VectorIndex(make_session_factory(engine), s.embed_model)
    index.reload()
    return Searcher(index, make_embedder(s))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m growwbuddy.retrieval.debug")
    parser.add_argument("query")
    parser.add_argument("--scheme", choices=SCHEME_KEYS)
    parser.add_argument("-k", type=int, default=5)
    args = parser.parse_args(argv)
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]

    scheme = args.scheme or resolve_scheme(args.query)
    if scheme is None:
        print(
            f"error: the query names no single scheme; pass --scheme ({', '.join(SCHEME_KEYS)})",
            file=sys.stderr,
        )
        return 2
    try:
        searcher = open_searcher()
        hits = searcher.search(args.query, scheme, args.k)
    except (IndexNotReadyError, MixedIndexError, EmbedderUnavailableError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    print(f"scheme: {scheme}\n")
    for rank, hit in enumerate(hits, 1):
        c = hit.chunk
        print(f"{rank}. {hit.score:.3f}  {c.chunk_id}")
        print(f"   {c.text}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
