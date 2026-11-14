"""Job summary and alert issue text for the daily refresh (task 7.5).

    python -m growwbuddy.ingest.notify summary OUT_DIR/result.json
    python -m growwbuddy.ingest.notify failure OUT_DIR/result.json --run-url URL
    python -m growwbuddy.ingest.notify facts   OUT_DIR/result.json --run-url URL

Prints Markdown. The text holds only public fund data (scheme names, fact values, gate
results): never user data, page text or secrets. `failure` also works when `result.json`
is missing (the job failed before the gates ran).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

FAILURE_TITLE = "Daily data refresh failed"
FACTS_TITLE_PREFIX = "Scheme facts changed"


def _md(value: object) -> str:
    """One table cell: no pipes or newlines; code-free."""
    return str(value).replace("|", "\\|").replace("\n", " ") if value is not None else "—"


def summary_markdown(result: dict[str, Any]) -> str:
    lines = [f"## Daily data refresh: {result.get('outcome', 'unknown')}", ""]
    if result.get("note"):
        lines += [str(result["note"]), ""]
    gates = result.get("gates") or []
    if gates:
        lines += ["| Gate | Result | Detail |", "|---|---|---|"]
        lines += [
            f"| {_md(g['name'])} | {'pass' if g['ok'] else '**FAIL**'} | {_md(g['detail'])} |"
            for g in gates
        ]
        lines.append("")
    sources = result.get("sources") or []
    if sources:
        lines += ["| Source | Status | Detail |", "|---|---|---|"]
        lines += [
            f"| {_md(s['source_id'])} | {_md(s['status'])} | {_md(s['detail'])} |" for s in sources
        ]
        lines.append("")
    warnings = [
        f"- {r['source_id']}: {w}" for r in result.get("linkcheck") or [] for w in r["warnings"]
    ]
    if warnings:
        lines += ["**Link check warnings**", "", *warnings, ""]
    changes = result.get("fact_changes") or []
    if changes:
        lines += ["**Fact changes** (review needed)", "", *_change_table(changes), ""]
    review = result.get("needs_review") or []
    if review:
        lines += [f"**Stored unverified, review needed:** {', '.join(review)}", ""]
    manifest = result.get("manifest") or {}
    if manifest:
        lines.append(
            f"Chunks {manifest.get('chunk_count')}, facts {manifest.get('fact_count')}, "
            f"model {manifest.get('embed_model')}, checked at {manifest.get('checked_at')}."
        )
    return "\n".join(lines).rstrip() + "\n"


def _change_table(changes: list[dict[str, Any]]) -> list[str]:
    return [
        "| Scheme | Fact | Old | New |",
        "|---|---|---|---|",
        *(
            f"| {_md(c['scheme'])} | {_md(c['fact_key'])} | {_md(c['old'])} | {_md(c['new'])} |"
            for c in changes
        ),
    ]


def failure_body(result: dict[str, Any] | None, run_url: str | None) -> str:
    where = f" in [this run]({run_url})" if run_url else ""
    lines = [
        f"The daily data refresh failed{where}.",
        "Nothing was published; the app keeps answering from the previous data.",
        "",
    ]
    if result is None:
        lines.append("The job stopped before the quality gates ran; see the run log.")
    else:
        failed = [g for g in result.get("gates") or [] if not g["ok"]]
        if failed:
            lines += ["**Failing gates**", ""]
            lines += [f"- `{g['name']}`: {g['detail']}" for g in failed]
        else:
            lines.append("No gate failed; a later step (publishing) failed. See the run log.")
    lines += ["", "Re-run from Actions → Refresh data → Run workflow once it's fixed."]
    return "\n".join(lines) + "\n"


def facts_body(result: dict[str, Any], run_url: str | None) -> str:
    changes = result.get("fact_changes") or []
    review = result.get("needs_review") or []
    run = f" ([run]({run_url}))" if run_url else ""
    lines = [
        f"The daily refresh{run} published scheme facts that need a person to check them. "
        "Please check each value against the live Groww page and record the review with "
        "`python -m growwbuddy.facts review --reviewer NAME` (EV-2-12).",
    ]
    if changes:
        lines += ["", "**Changed** (their review was cleared)", "", *_change_table(changes)]
    if review:
        lines += [
            "",
            "**Exit-load wording not recognised** (stored unverified: the rules don't match "
            "it and the refresh runs without an LLM). The facts table won't use them until "
            "a value is entered with `python -m growwbuddy.facts set` or the rules learn "
            "the wording:",
            "",
            *(f"- {label}" for label in review),
        ]
    missing = (result.get("fact_diff") or {}).get("kept_missing") or []
    if missing:
        lines += ["", "No longer on the page (last value kept): " + ", ".join(missing)]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m growwbuddy.ingest.notify")
    parser.add_argument("kind", choices=["summary", "failure", "facts"])
    parser.add_argument("result", type=Path)
    parser.add_argument("--run-url")
    args = parser.parse_args(argv)
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    try:
        result: dict[str, Any] | None = json.loads(args.result.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        result = None
    if args.kind == "failure":
        print(failure_body(result, args.run_url), end="")
        return 0
    if result is None:
        print(f"error: cannot read {args.result}", file=sys.stderr)
        return 1
    text = summary_markdown(result) if args.kind == "summary" else facts_body(result, args.run_url)
    print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
