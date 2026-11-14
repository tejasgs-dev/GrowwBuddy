"""CLI: python -m growwbuddy.ingest [--source ID] [--force] [--report] [--no-fetch]
       python -m growwbuddy.ingest --bundle OUT_DIR [--previous PREV_DIR] [--force]

Runs fetch -> hash check -> parse (Phase 1), then chunk -> embed -> index + facts (Phase 2).
`--bundle` builds a standalone index bundle for the daily refresh instead of writing to the
app database (Phase 7; see ingest/bundle.py).
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from growwbuddy.config import settings
from growwbuddy.ingest.coverage import format_report, load_pages
from growwbuddy.ingest.fetch import Fetcher
from growwbuddy.ingest.index import IndexReport, build_index
from growwbuddy.ingest.lock import IngestLockedError, ingest_lock
from growwbuddy.ingest.manifest import Manifest, ManifestError, load_manifest
from growwbuddy.ingest.pipeline import run_ingest
from growwbuddy.llm.client import GroqClient
from growwbuddy.retrieval.embed import EmbedderUnavailableError, make_embedder
from growwbuddy.retrieval.headers import HeadersError, RetrievalHeaders, load_headers
from growwbuddy.store.db import init_db, make_engine, make_session_factory


def _print_index_report(r: IndexReport) -> None:
    if r.skipped:
        print(f"index      up to date ({r.chunks} chunks, generation {r.generation})")
        return
    print(f"index      rebuilt: {r.chunks} chunks, generation {r.generation}")
    print(
        f"facts      {len(r.facts_new)} new, {len(r.facts_changed)} changed, "
        f"{r.facts_unchanged} unchanged"
    )
    for label in r.facts_changed:
        print(f"  changed  {label}  (old value in scheme_facts_history)")
    for label in r.facts_kept_missing:
        print(f"  missing  {label}  (no longer on the page; last value kept)")
    if r.unverified:
        print(f"  {len(r.unverified)} unverified: {', '.join(r.unverified)}")
        print("  review with: python -m growwbuddy.facts review")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m growwbuddy.ingest",
        description="Fetch and parse the five Groww scheme pages, then rebuild the index.",
    )
    parser.add_argument(
        "--source", action="append", metavar="ID", help="only fetch this source (repeatable)"
    )
    parser.add_argument(
        "--force", action="store_true", help="re-parse and re-index even if unchanged"
    )
    parser.add_argument(
        "--report", action="store_true", help="print the coverage report and exit (no fetch)"
    )
    parser.add_argument(
        "--no-fetch", action="store_true", help="skip fetching; index the existing parsed pages"
    )
    parser.add_argument(
        "--bundle",
        type=Path,
        metavar="OUT_DIR",
        help="build a daily index bundle in OUT_DIR (Phase 7); the app database is untouched",
    )
    parser.add_argument(
        "--previous",
        type=Path,
        metavar="PREV_DIR",
        help="with --bundle: folder holding the previous release's manifest.json and "
        "bundle.sqlite (change detection and fact diff)",
    )
    args = parser.parse_args(argv)
    if args.bundle is None and args.previous is not None:
        parser.error("--previous needs --bundle")
    if args.bundle is not None and (args.source or args.no_fetch or args.report):
        parser.error("--bundle always fetches all five sources")

    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]

    if args.report:
        print(format_report(load_pages(settings.parsed_dir)))
        return 0

    try:
        manifest = load_manifest(settings.corpus_dir / "sources.yaml")
        if args.source:
            for sid in args.source:
                manifest.get(sid)
        headers = load_headers()
    except (ManifestError, HeadersError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except KeyError as e:
        print(f"error: unknown source id {e}", file=sys.stderr)
        return 2

    if args.bundle is not None:
        return _run_bundle(args.bundle, args.previous, args.force, manifest, headers)

    engine = make_engine(settings.db_url)
    init_db(engine)
    session_factory = make_session_factory(engine)
    failed = 0
    try:
        with ingest_lock(settings.parsed_dir.parent / "ingest.lock"):
            if not args.no_fetch:
                with Fetcher() as fetcher:
                    outcomes = run_ingest(
                        manifest,
                        fetcher=fetcher,
                        session_factory=session_factory,
                        raw_dir=settings.corpus_dir / "raw",
                        parsed_dir=settings.parsed_dir,
                        only=args.source,
                        force=args.force,
                        raw_keep=settings.raw_keep,
                    )
                for o in outcomes:
                    print(f"{o.status:<10} {o.source_id:<28} {o.detail}")
                failed = sum(o.status == "failed" for o in outcomes)
                print(f"\n{len(outcomes) - failed}/{len(outcomes)} sources ok\n")

            pages = load_pages(settings.parsed_dir)
            if not pages:
                print("error: no parsed pages to index", file=sys.stderr)
                return 1
            report = build_index(
                pages,
                session_factory=session_factory,
                embedder=make_embedder(settings),
                headers=headers,
                llm=GroqClient(settings),
                force=args.force,
            )
            _print_index_report(report)
    except IngestLockedError as e:
        print(f"error: {e}", file=sys.stderr)
        return 3
    except EmbedderUnavailableError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 1 if failed else 0


def _run_url() -> str | None:
    server, repo, run = (os.environ.get(k) for k in
                         ("GITHUB_SERVER_URL", "GITHUB_REPOSITORY", "GITHUB_RUN_ID"))  # fmt: skip
    return f"{server}/{repo}/actions/runs/{run}" if server and repo and run else None


def _run_bundle(
    out_dir: Path,
    previous: Path | None,
    force: bool,
    manifest: Manifest,
    headers: RetrievalHeaders,
) -> int:
    import logging

    from growwbuddy.ingest.bundle import build_bundle

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    # The daily workflow has no GROQ_API_KEY: then there is no LLM fallback, and exit-load
    # wording the rules don't recognise is stored unverified and listed for review (7.9).
    key = settings.groq_api_key
    llm = GroqClient(settings) if key is not None and key.get_secret_value().strip() else None
    try:
        with Fetcher() as fetcher:
            result = build_bundle(
                out_dir,
                manifest=manifest,
                fetcher=fetcher,
                embedder=make_embedder(settings),
                headers=headers,
                expected_embed_model=settings.embed_model,
                llm=llm,
                previous_dir=previous,
                force=force,
                git_sha=os.environ.get("GITHUB_SHA"),
                run_url=_run_url(),
            )
    except EmbedderUnavailableError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    for o in result.sources:
        print(f"{o.status:<10} {o.source_id:<28} {o.detail}")
    for g in result.gates:
        print(f"gate {'pass' if g.ok else 'FAIL'}  {g.name:<16} {g.detail}")
    print(f"\noutcome={result.outcome}  ({result.note})")
    return 0 if result.outcome != "failed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
