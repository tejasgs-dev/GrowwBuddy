"""Link check (task 6.8): the five Groww URLs still resolve and still show the right scheme.

    python -m growwbuddy.ingest.linkcheck          # what `make linkcheck` runs

For each source in `corpus/sources.yaml`:

1. **Resolves:** the page fetches with HTTP 200 through the normal fetcher (generic
   User-Agent, retries, every redirect hop domain-checked). A 404/410 or any other failure
   is a hard failure (EC-ING-06).
2. **Not moved:** the final URL after redirects is the manifest URL. A redirect to another
   Groww page (a renamed, merged or delisted scheme, or a search page) is a hard failure
   (EC-ING-05).
3. **Right scheme:** the page's embedded fund data passes the parser's identity checks
   (slug, Direct plan, Growth option, HDFC). A bot-check or error page has no fund data and
   fails here too.
4. **Displayed expense ratio** (a warning, never a failure): the value in the page's
   rendered "Expense ratio" box is compared with the stored `expense_ratio_base` and
   `expense_ratio_direct`. A match with either is fine; matching neither is logged with the
   scheme, the displayed value, both stored values and whether the "Exclusive of GST" label
   is shown. The box is read from rendered HTML, which is fragile, so "box not found" is
   its own warning and never counts as a mismatch.

No page text is logged or printed: only statuses, values and labels. Phase 7 runs this in
the daily refresh workflow (task 7.8).
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Literal

from bs4 import BeautifulSoup, Tag
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from growwbuddy.ingest.fetch import Fetcher, FetchError
from growwbuddy.ingest.manifest import Manifest, SourceEntry
from growwbuddy.ingest.parse_groww import ParseError, extract_fund_data
from growwbuddy.store.models import SchemeFact

log = logging.getLogger(__name__)

Status = Literal["ok", "warning", "failed"]

_PERCENT = re.compile(r"(\d+(?:\.\d+)?)\s*%")
_GST_LABEL = re.compile(r"exclusive of gst", re.IGNORECASE)
_BOX_BUTTON_LABEL = "Information - Expense ratio"


@dataclass(frozen=True)
class DisplayedTer:
    value: str | None  # e.g. "0.79%"; None when the box wasn't found
    gst_label: bool  # "Exclusive of GST" shown next to it


@dataclass
class LinkResult:
    source_id: str
    scheme: str
    status: Status = "ok"
    problems: list[str] = field(default_factory=list)  # hard failures
    warnings: list[str] = field(default_factory=list)

    def fail(self, problem: str) -> None:
        self.status = "failed"
        self.problems.append(problem)

    def warn(self, warning: str) -> None:
        if self.status == "ok":
            self.status = "warning"
        self.warnings.append(warning)


def _same_url(a: str, b: str) -> bool:
    return a.rstrip("/").lower() == b.rstrip("/").lower()


def displayed_expense_ratio(html: str) -> DisplayedTer:
    """The value in the page's "Expense ratio" box, read from the rendered HTML.

    The box is a label (with an info button) followed by the value. Found by the info
    button's aria-label, else by a label whose own text is "Expense ratio".
    """
    soup = BeautifulSoup(html, "html.parser")
    for script in soup.find_all(["script", "style"]):
        script.decompose()  # never read the embedded JSON as if it were the box
    label: Tag | None = None
    button = soup.find("button", attrs={"aria-label": _BOX_BUTTON_LABEL})
    if isinstance(button, Tag) and isinstance(button.parent, Tag):
        label = button.parent
    else:
        for el in soup.find_all(string=re.compile(r"^\s*Expense ratio\s*$", re.IGNORECASE)):
            if isinstance(el.parent, Tag):
                label = el.parent
                break
    if label is None or not isinstance(label.parent, Tag):
        return DisplayedTer(None, False)
    box = label.parent
    value = None
    for sibling in label.find_next_siblings():
        m = _PERCENT.search(sibling.get_text(" ", strip=True))
        if m:
            value = f"{m.group(1)}%"
            break
    around = box.parent.get_text(" ", strip=True) if isinstance(box.parent, Tag) else ""
    return DisplayedTer(value, bool(_GST_LABEL.search(around)))


def _pct(value: str | None) -> Decimal | None:
    if not value:
        return None
    m = _PERCENT.search(value)
    if not m:
        return None
    try:
        return Decimal(m.group(1))
    except InvalidOperation:
        return None


def stored_expense_ratios(
    session_factory: sessionmaker[Session], scheme: str
) -> tuple[str | None, str | None]:
    """(base, total) as stored in `scheme_facts`, verified or not."""
    with session_factory() as session:
        rows = session.scalars(
            select(SchemeFact).where(
                SchemeFact.scheme == scheme,
                SchemeFact.fact_key.in_(("expense_ratio_base", "expense_ratio_direct")),
            )
        ).all()
    by_key = {r.fact_key: r.value_text for r in rows}
    return by_key.get("expense_ratio_base"), by_key.get("expense_ratio_direct")


def check_source(
    source: SourceEntry,
    fetcher: Fetcher,
    stored: Callable[[str], tuple[str | None, str | None]],
) -> LinkResult:
    result = LinkResult(source.id, source.scheme)
    try:
        page = fetcher.fetch(source)
    except FetchError as e:
        result.fail(f"fetch failed: {e}")
        return result
    if not _same_url(page.final_url, source.url):
        # On-domain, so it was safe to follow, but it's no longer the scheme's own page.
        result.fail(f"redirected to a different page: {page.final_url}")
        return result
    try:
        extract_fund_data(page.html, source)
    except ParseError as e:
        result.fail(f"wrong or missing scheme data: {e}")
        return result

    shown = displayed_expense_ratio(page.html)
    base, total = stored(source.scheme)
    if shown.value is None:
        result.warn("expense ratio box not found in the rendered page")
        log.warning("linkcheck expense box not found scheme=%s", source.scheme)
    else:
        value = _pct(shown.value)
        if value is None or value not in {_pct(base), _pct(total)} - {None}:
            result.warn(
                f"displayed expense ratio {shown.value} matches neither stored figure "
                f"(base {base}, total {total}; 'Exclusive of GST' label "
                f"{'shown' if shown.gst_label else 'not shown'})"
            )
            log.warning(
                "linkcheck expense ratio mismatch scheme=%s displayed=%s stored_base=%s "
                "stored_total=%s gst_label=%s",
                source.scheme,
                shown.value,
                base,
                total,
                shown.gst_label,
            )
    if result.status == "failed":
        log.warning("linkcheck failed source=%s problems=%s", source.id, result.problems)
    return result


def run_linkcheck(
    manifest: Manifest,
    fetcher: Fetcher,
    session_factory: sessionmaker[Session],
) -> list[LinkResult]:
    def stored(scheme: str) -> tuple[str | None, str | None]:
        return stored_expense_ratios(session_factory, scheme)

    return [check_source(s, fetcher, stored) for s in manifest.sources]


def main(argv: list[str] | None = None) -> int:
    from growwbuddy.config import settings
    from growwbuddy.ingest.manifest import ManifestError, load_manifest
    from growwbuddy.store.db import init_db, make_engine, make_session_factory

    parser = argparse.ArgumentParser(
        prog="python -m growwbuddy.ingest.linkcheck",
        description="Check the five Groww URLs still resolve and show the expected scheme.",
    )
    parser.parse_args(argv)
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    try:
        manifest = load_manifest(settings.corpus_dir / "sources.yaml")
    except ManifestError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    engine = make_engine(settings.db_url)
    init_db(engine)
    with Fetcher() as fetcher:
        results = run_linkcheck(manifest, fetcher, make_session_factory(engine))
    for r in results:
        print(f"{r.status:<8} {r.source_id}")
        for p in r.problems:
            print(f"  FAIL  {p}")
        for w in r.warnings:
            print(f"  warn  {w}")
    failed = sum(r.status == "failed" for r in results)
    print(f"\n{len(results) - failed}/{len(results)} sources ok")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
