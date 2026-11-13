"""Tune the retrieval low-confidence cutoff offline (task 6.3). No Groq calls.

    python -m growwbuddy.eval.cutoff [--max-false-rate 0.01]

The cutoff only matters for a question that matches no verified fact (a fact match always
answers, task 3.8). It says "not found" when the top score is below `min_score`, or the
gap between the top two scores is below `min_gap`.

- **Positives** are questions the corpus answers: the Phase 2 retrieval query sets (one
  per scheme, section and wording) and the golden fact items. A cut positive is a false
  "not found".
- **Negatives** are golden and held-out items marked `not_in_corpus` (NAV, AUM, returns,
  holdings). Most of these are caught earlier by the router rules; the ones worded to get
  past the rules are the cases the cutoff exists for.

Queries are embedded as written. In production the router rewrites them first, which fixes
typos and resolves follow-ups, so this is a slightly pessimistic view of the positives.

It prints the score distributions and the (min_score, min_gap) pairs that catch the most
negatives while cutting at most `--max-false-rate` of the positives, and recommends one.
Put the chosen values in `.env` (GROWWBUDDY_RETRIEVAL_MIN_SCORE, _MIN_GAP), then confirm
them on a live eval run.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path

from growwbuddy.eval.golden import EVAL_DIR, GOLDEN_FILE, HOLDOUT_FILE, load_items
from growwbuddy.pipeline.router_rules import decide
from growwbuddy.retrieval.retriever import Retriever


@dataclass(frozen=True)
class Probe:
    text: str
    scheme: str
    positive: bool
    source: str  # where the probe came from
    rule_caught: bool = False  # the router rules answer it before retrieval


@dataclass(frozen=True)
class Scored:
    probe: Probe
    fact_path: bool  # matched a verified fact: the cutoff never applies
    top: float | None
    gap: float | None


def load_probes() -> list[Probe]:
    probes: list[Probe] = []
    for name in ("retrieval_queries.jsonl", "retrieval_queries_heldout.jsonl"):
        path = EVAL_DIR / name
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                q = json.loads(line)
                probes.append(Probe(q["query"], q["scheme"], True, name))
    for path in (GOLDEN_FILE, HOLDOUT_FILE):
        for item in load_items(path):
            e = item.expect
            if e.scheme is None:
                continue
            if e.not_in_corpus:
                caught = decide(item.last) is not None
                probes.append(Probe(item.last, e.scheme, False, item.id, caught))
            elif len(item.turns) == 1 and e.type == ["answer"]:
                probes.append(Probe(item.last, e.scheme, True, item.id))
    return probes


def score(probes: list[Probe], retriever: Retriever) -> list[Scored]:
    out = []
    for p in probes:
        r = retriever.retrieve(p.text, p.scheme)
        fact_path = any(i.kind == "fact" for i in r.items)
        out.append(Scored(p, fact_path, r.top_score, r.gap))
    return out


def is_cut(s: Scored, min_score: float | None, min_gap: float | None) -> bool:
    if s.fact_path:
        return False
    if s.top is None:
        return True
    if min_score is not None and s.top < min_score:
        return True
    return min_gap is not None and s.gap is not None and s.gap < min_gap


@dataclass(frozen=True)
class Option:
    min_score: float | None
    min_gap: float | None
    false_cuts: int
    caught: int  # negatives (not caught by rules) that the cutoff turns into "not found"
    caught_all: int  # all negatives, including rule-caught ones


def evaluate(scored: list[Scored], max_false_rate: float) -> list[Option]:
    positives = [s for s in scored if s.probe.positive]
    negatives = [s for s in scored if not s.probe.positive]
    open_negs = [s for s in negatives if not s.probe.rule_caught]
    tops = sorted({round(s.top, 3) for s in scored if s.top is not None and not s.fact_path})
    gaps = sorted({round(s.gap, 3) for s in scored if s.gap is not None and not s.fact_path})
    score_grid: list[float | None] = [None, *(t + 0.0005 for t in tops)]
    gap_grid: list[float | None] = [None, *(g + 0.0005 for g in gaps)]
    allowed = int(max_false_rate * len(positives))
    options = []
    for ms in score_grid:
        for mg in gap_grid:
            false_cuts = sum(is_cut(s, ms, mg) for s in positives)
            if false_cuts > allowed:
                continue
            options.append(
                Option(
                    None if ms is None else round(ms, 4),
                    None if mg is None else round(mg, 4),
                    false_cuts,
                    sum(is_cut(s, ms, mg) for s in open_negs),
                    sum(is_cut(s, ms, mg) for s in negatives),
                )
            )
    # Most negatives caught, then fewest false cuts, then the loosest (lowest) cutoffs.
    # Rule-caught negatives never reach retrieval, so they don't rank options.
    options.sort(key=lambda o: (-o.caught, o.false_cuts, o.min_score or 0, o.min_gap or 0))
    return options


def _fmt(v: float | None) -> str:
    return "-" if v is None else f"{v:.3f}"


def main(argv: list[str] | None = None) -> int:
    from growwbuddy.config import settings
    from growwbuddy.retrieval.embed import make_embedder
    from growwbuddy.retrieval.vector import VectorIndex
    from growwbuddy.store.db import init_db, make_engine, make_session_factory

    parser = argparse.ArgumentParser(prog="python -m growwbuddy.eval.cutoff")
    parser.add_argument(
        "--max-false-rate",
        type=float,
        default=0.01,
        help="largest share of answerable questions the cutoff may turn into 'not found'",
    )
    parser.add_argument("--json", type=Path, help="also write the scores to this file")
    args = parser.parse_args(argv)
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]

    engine = make_engine(settings.db_url)
    init_db(engine)
    sf = make_session_factory(engine)
    retriever = Retriever(VectorIndex(sf, settings.embed_model), make_embedder(settings), sf)
    scored = score(load_probes(), retriever)

    pos = [s for s in scored if s.probe.positive]
    neg = [s for s in scored if not s.probe.positive]
    pos_open = [s for s in pos if not s.fact_path]
    print(f"{len(pos)} answerable questions ({len(pos) - len(pos_open)} answered from facts, "
          f"so never cut); {len(neg)} not-in-corpus questions")  # fmt: skip
    if pos_open:
        low = sorted(pos_open, key=lambda s: s.top or 0)[:5]
        print("lowest top scores among answerable questions without a fact match:")
        for s in low:
            print(f"  top {_fmt(s.top)} gap {_fmt(s.gap)}  [{s.probe.scheme}] {s.probe.text!r}")
    print("not-in-corpus questions:")
    for s in neg:
        where = "rules" if s.probe.rule_caught else ("facts" if s.fact_path else "retrieval")
        print(
            f"  {s.probe.source:<11} top {_fmt(s.top)} gap {_fmt(s.gap)} reaches={where:<9} "
            f"{s.probe.text!r}"
        )

    options = evaluate(scored, args.max_false_rate)
    open_total = sum(1 for s in neg if not s.probe.rule_caught)
    print(
        f"\nbest cutoffs (at most {args.max_false_rate:.0%} of answerable questions cut; "
        f"{open_total} negatives get past the rules):"
    )
    print("  min_score  min_gap  false_not_found  negatives_caught  (incl. rule-caught)")
    for o in options[:8]:
        print(
            f"  {_fmt(o.min_score):>9}  {_fmt(o.min_gap):>7}  {o.false_cuts:>8}/{len(pos):<6}"
            f"  {o.caught:>8}/{open_total:<6}  {o.caught_all}/{len(neg)}"
        )
    if not options or options[0].caught == 0:
        print(
            "\nrecommended: leave GROWWBUDDY_RETRIEVAL_MIN_SCORE and _MIN_GAP unset. No cutoff "
            "catches a not-in-corpus question that gets past the rules without also cutting "
            "answerable ones; those rely on the router and the generator's answerable=false."
        )
    else:
        best = options[0]
        print(
            f"\nrecommended: GROWWBUDDY_RETRIEVAL_MIN_SCORE={_fmt(best.min_score)} "
            f"GROWWBUDDY_RETRIEVAL_MIN_GAP={_fmt(best.min_gap)} "
            f"(false not-found {best.false_cuts}/{len(pos)} = {best.false_cuts / len(pos):.1%})"
        )
    if args.json:
        args.json.write_text(
            json.dumps(
                [
                    {"text": s.probe.text, "scheme": s.probe.scheme, "positive": s.probe.positive,
                     "source": s.probe.source, "rule_caught": s.probe.rule_caught,
                     "fact_path": s.fact_path, "top": s.top, "gap": s.gap}
                    for s in scored
                ],  # fmt: skip
                indent=1,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
