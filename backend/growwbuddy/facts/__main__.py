"""CLI: python -m growwbuddy.facts {list,review,verify,set}

list    [--scheme KEY] [--unverified]            print facts with their evidence
review  --reviewer NAME [--scheme KEY] [--all]   go through facts one by one
verify  SCHEME FACT_KEY --reviewer NAME          confirm one fact
set     SCHEME FACT_KEY VALUE --reviewer NAME    enter a corrected value
"""

from __future__ import annotations

import argparse
import sys

from growwbuddy.config import settings
from growwbuddy.facts.review import (
    ReviewError,
    describe,
    list_facts,
    review_loop,
    set_manual,
    verify_fact,
)
from growwbuddy.store.db import init_db, make_engine, make_session_factory


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m growwbuddy.facts")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list", help="print facts with their evidence")
    p_list.add_argument("--scheme")
    p_list.add_argument("--unverified", action="store_true", help="only unverified facts")

    p_review = sub.add_parser(
        "review", help="go through facts not yet confirmed by a person (default) or all"
    )
    p_review.add_argument("--reviewer", required=True)
    p_review.add_argument("--scheme")
    p_review.add_argument("--all", action="store_true", help="include already-reviewed facts")

    p_verify = sub.add_parser("verify", help="confirm one fact matches the live page")
    p_verify.add_argument("scheme")
    p_verify.add_argument("fact_key")
    p_verify.add_argument("--reviewer", required=True)

    p_set = sub.add_parser("set", help="enter a corrected value (extracted_by = manual)")
    p_set.add_argument("scheme")
    p_set.add_argument("fact_key")
    p_set.add_argument("value")
    p_set.add_argument("--reviewer", required=True)

    args = parser.parse_args(argv)
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]

    engine = make_engine(settings.db_url)
    init_db(engine)
    with make_session_factory(engine)() as session:
        try:
            if args.cmd == "list":
                rows = list_facts(session, scheme=args.scheme, unverified_only=args.unverified)
                for row in rows:
                    print(describe(row) + "\n")
                print(f"{len(rows)} facts, {sum(not r.verified for r in rows)} unverified")
            elif args.cmd == "review":
                rows = list_facts(session, scheme=args.scheme, pending_only=not args.all)
                if not rows:
                    print("nothing to review")
                    return 0
                confirmed, corrected = review_loop(session, rows, args.reviewer)
                print(f"\n{confirmed} confirmed, {corrected} corrected")
            elif args.cmd == "verify":
                verify_fact(session, args.scheme, args.fact_key, args.reviewer)
                session.commit()
                print(f"verified {args.scheme}/{args.fact_key}")
            elif args.cmd == "set":
                set_manual(session, args.scheme, args.fact_key, args.value, args.reviewer)
                session.commit()
                print(f"set {args.scheme}/{args.fact_key} = {args.value}")
        except ReviewError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
