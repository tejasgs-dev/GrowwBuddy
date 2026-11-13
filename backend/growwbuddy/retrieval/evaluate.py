"""Measure filtered retrieval accuracy on a query set (Phase 2 exit check).

    python -m growwbuddy.retrieval.evaluate [eval/retrieval_queries.jsonl] [--show-misses]

Each line: {"query", "scheme", "expected_section", "type", "wording"}. The search is
filtered to `scheme`, as in production. Uses the real embedding model; no API calls.
Exits non-zero below the targets (top-1 >= 90%, top-3 >= 97%).
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from growwbuddy.retrieval.debug import Searcher, open_searcher

DEFAULT_FILE = Path(__file__).resolve().parents[2] / "eval" / "retrieval_queries.jsonl"
TOP1_TARGET = 0.90
TOP3_TARGET = 0.97


@dataclass(frozen=True)
class Query:
    query: str
    scheme: str
    expected_section: str
    type: str
    wording: str


@dataclass(frozen=True)
class Outcome:
    q: Query
    ranked_sections: tuple[str, ...]

    @property
    def rank(self) -> int | None:
        try:
            return self.ranked_sections.index(self.q.expected_section) + 1
        except ValueError:
            return None


def load_queries(path: Path) -> list[Query]:
    return [
        Query(**json.loads(line))
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def run(queries: list[Query], searcher: Searcher, k: int = 5) -> list[Outcome]:
    return [
        Outcome(q, tuple(h.chunk.section for h in searcher.search(q.query, q.scheme, k)))
        for q in queries
    ]


def summarise(outcomes: list[Outcome]) -> dict[str, float]:
    n = len(outcomes) or 1
    return {
        "top1": sum(o.rank == 1 for o in outcomes) / n,
        "top3": sum(o.rank is not None and o.rank <= 3 for o in outcomes) / n,
        "top5": sum(o.rank is not None for o in outcomes) / n,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m growwbuddy.retrieval.evaluate")
    parser.add_argument("file", nargs="?", type=Path, default=DEFAULT_FILE)
    parser.add_argument("--show-misses", action="store_true", help="list every non-top-1 query")
    args = parser.parse_args(argv)
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]

    queries = load_queries(args.file)
    outcomes = run(queries, open_searcher())
    overall = summarise(outcomes)
    print(f"{len(outcomes)} queries from {args.file.name}")
    print(
        f"filtered top-1 {overall['top1']:.1%}  top-3 {overall['top3']:.1%}  "
        f"top-5 {overall['top5']:.1%}"
    )
    for group in ("type", "wording"):
        print(f"\nby {group}:")
        for name in sorted({getattr(o.q, group) for o in outcomes}):
            sub = [o for o in outcomes if getattr(o.q, group) == name]
            s = summarise(sub)
            print(f"  {name:<20} n={len(sub):<4} top-1 {s['top1']:.0%}  top-3 {s['top3']:.0%}")

    misses = [o for o in outcomes if o.rank != 1]
    if misses:
        confusions = Counter((o.q.expected_section, o.ranked_sections[0]) for o in misses)
        print("\nmost common confusions (expected -> got):")
        for (exp, got), n in confusions.most_common(5):
            print(f"  {exp} -> {got}: {n}")
    if args.show_misses:
        print("\nmisses:")
        for o in misses:
            print(f"  rank {o.rank}  [{o.q.scheme}] {o.q.query!r}  got {o.ranked_sections[:3]}")

    ok = overall["top1"] >= TOP1_TARGET and overall["top3"] >= TOP3_TARGET
    print(f"\n{'PASS' if ok else 'FAIL'} (targets: top-1 >= 90%, top-3 >= 97%)")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
