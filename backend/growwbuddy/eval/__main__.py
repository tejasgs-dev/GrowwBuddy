"""CLI: run the golden set against the live model (task 6.2, eval.md §8.5).

    make eval                                         # python -m growwbuddy.eval
    python -m growwbuddy.eval --category advisory     # one category (comma-separated list)
    python -m growwbuddy.eval --ids G-TER-001,G-FUP-003
    python -m growwbuddy.eval --limit 10              # first 10 selected items
    python -m growwbuddy.eval --repeat 3              # before a release: flaky items fail
    python -m growwbuddy.eval --resume                # continue the latest run
    python -m growwbuddy.eval --set holdout           # held-out items: Phase 6 gate only
    python -m growwbuddy.eval --no-llm                # only the rule-handled items; free

Uses real Groq calls (needs GROQ_API_KEY; costs usage). Never run it in CI (EC-EVAL-04).
Writes eval/reports/<timestamp>/: progress.jsonl, eval.log, meta.json, report.json and
report.md. Exits 0 only when the run is complete and every blocking metric meets its target.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from growwbuddy.config import DATA_DIR, MissingApiKeyError, settings
from growwbuddy.eval.golden import EVAL_DIR, GOLDEN_FILE, HOLDOUT_FILE, load_items
from growwbuddy.eval.report import write_report
from growwbuddy.eval.runner import (
    ItemRecord,
    RunOptions,
    Throttle,
    build_harness,
    configure_eval_logging,
    model_turns,
    read_progress,
    run_items,
    run_metadata,
)

REPORTS_DIR = EVAL_DIR / "reports"


def _latest_run() -> Path | None:
    runs = sorted(p for p in REPORTS_DIR.glob("*") if (p / "meta.json").exists())
    return runs[-1] if runs else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m growwbuddy.eval", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)  # fmt: skip
    parser.add_argument("--set", choices=("golden", "holdout"), default="golden")
    parser.add_argument("--file", type=Path, help="item file (overrides --set)")
    parser.add_argument("--category", help="comma-separated categories to run")
    parser.add_argument("--ids", help="comma-separated item IDs to run")
    parser.add_argument("--limit", type=int, help="run only the first N selected items")
    parser.add_argument("--repeat", type=int, default=1, help="run each item N times")
    parser.add_argument(
        "--resume", nargs="?", const="latest", metavar="RUN_DIR", help="continue a run"
    )
    parser.add_argument(
        "--no-llm", action="store_true", help="run only items the rules handle (no API usage)"
    )
    parser.add_argument("--reserve", type=int, default=20_000, help="daily tokens to leave unused")
    parser.add_argument("--share", type=float, default=0.8, help="share of per-minute limits")
    parser.add_argument("--max-wait", type=float, default=90.0, help="longest retry-after (s)")
    parser.add_argument("--eval-db", default=f"sqlite:///{(DATA_DIR / 'eval.db').as_posix()}")
    args = parser.parse_args(argv)
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]

    # ---- the run directory, and the item selection (a resumed run keeps its own)
    if args.resume:
        run_dir = _latest_run() if args.resume == "latest" else Path(args.resume)
        if run_dir is None or not (run_dir / "meta.json").exists():
            print("error: no run to resume", file=sys.stderr)
            return 2
        meta = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
        item_file = EVAL_DIR / meta["item_file"]
        selected_ids = meta["selected_ids"]
        repeat = int(meta["repeat"])
        no_llm = bool(meta["no_llm"])
    else:
        item_file = args.file or (HOLDOUT_FILE if args.set == "holdout" else GOLDEN_FILE)
        items = load_items(item_file)
        if args.category:
            wanted = {c.strip() for c in args.category.split(",")}
            items = [i for i in items if i.category in wanted]
        if args.ids:
            wanted = {i.strip() for i in args.ids.split(",")}
            items = [i for i in items if i.id in wanted]
        if args.limit:
            items = items[: args.limit]
        selected_ids = [i.id for i in items]
        repeat, no_llm = max(1, args.repeat), args.no_llm
        run_dir = REPORTS_DIR / datetime.now(UTC).strftime("%Y-%m-%d_%H%M%S")
        meta = None

    by_id = {i.id: i for i in load_items(item_file)}
    items = [by_id[i] for i in selected_ids if i in by_id]
    if not items:
        print("error: no items selected", file=sys.stderr)
        return 2
    runnable = [i for i in items if not (no_llm and model_turns(i))]
    if not no_llm:
        try:
            settings.require_groq_api_key()
        except MissingApiKeyError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2

    run_dir.mkdir(parents=True, exist_ok=True)
    opts = RunOptions(run_dir, repeat=repeat, no_llm=no_llm, share=args.share,
                      reserve=args.reserve, max_wait_s=args.max_wait)  # fmt: skip
    print(f"loading the index and embedder; run directory {run_dir}")
    harness, es = build_harness(settings, args.eval_db, no_llm, args.max_wait)
    configure_eval_logging(run_dir, harness.capture)
    if meta is None:
        meta = run_metadata(es, harness.ctx, item_file, opts)
        meta["selected_ids"] = selected_ids
        (run_dir / "meta.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")

    earlier = read_progress(run_dir)
    done = {(r.id, r.repeat) for r in earlier if r.status == "done"}
    today = datetime.now(UTC).date().isoformat()
    throttle = Throttle(
        tpm=es.groq_tpm,
        rpm=es.groq_rpm,
        tpd=es.groq_tpd,
        share=opts.share,
        reserve=opts.reserve,
        day_tokens=sum(
            r.tokens.get("total_tokens", 0) - r.tokens.get("cached_tokens", 0)
            for r in earlier
            if r.finished_at.startswith(today)
        ),
    )
    free = sum(1 for i in runnable if not model_turns(i))
    print(
        f"{len(runnable)} items x {repeat} ({free} rule-handled first, "
        f"{len(runnable) - free} need the model); {len(done)} already done"
    )

    progress = run_dir / "progress.jsonl"

    def on_record(rec: ItemRecord) -> None:
        with progress.open("a", encoding="utf-8") as f:
            f.write(rec.to_json() + "\n")

    stopped = asyncio.run(run_items(harness, items, opts, throttle, on_record=on_record, done=done))
    summary, partial = write_report(run_dir, runnable, expected_runs=len(runnable) * repeat)
    print(f"\nreport: {run_dir / 'report.md'}" + ("  (PARTIAL)" if partial else ""))
    for m in summary["metrics"]:
        state = "tracked" if m["ok"] is None else ("pass" if m["ok"] else "FAIL")
        print(f"  {state:<8} {m['name']}: {m['value']}  ({m['detail'] or m['target']})")
    if stopped != "complete":
        return 3
    return 0 if summary["blocking_passed"] and not partial else 1


if __name__ == "__main__":
    raise SystemExit(main())
