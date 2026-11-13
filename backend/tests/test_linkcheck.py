"""Link check (task 6.8; EV-6-06 automated part, EC-ING-05, 06). Mocked HTTP only."""

from __future__ import annotations

import logging
import re
from collections.abc import Callable

import httpx
import pytest
from sqlalchemy.orm import Session, sessionmaker

from growwbuddy.ingest.fetch import Fetcher
from growwbuddy.ingest.linkcheck import (
    check_source,
    displayed_expense_ratio,
    run_linkcheck,
    stored_expense_ratios,
)
from growwbuddy.ingest.manifest import Manifest, SourceEntry
from tests.conftest import FIXED_NOW, fixture_html, fund_json, page_with_fund

Handler = Callable[[httpx.Request], httpx.Response]
STORED = {
    "hdfc-mid-cap": ("0.60%", "0.74%"),
    "hdfc-small-cap": ("0.64%", "0.79%"),
}


def fetcher(handler: Handler) -> Fetcher:
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return Fetcher(client, sleep=lambda _s: None, clock=lambda: FIXED_NOW, max_attempts=1)


def stored(scheme: str) -> tuple[str | None, str | None]:
    return STORED.get(scheme, (None, None))


@pytest.fixture
def mid(manifest: Manifest) -> SourceEntry:
    return manifest.get("groww-hdfc-mid-cap")


def serve(html: str, status: int = 200) -> Handler:
    return lambda _req: httpx.Response(status, text=html)


def test_live_shaped_page_passes(mid: SourceEntry) -> None:
    r = check_source(mid, fetcher(serve(fixture_html(mid.id))), stored)
    assert r.status == "ok", (r.problems, r.warnings)


def test_EV_6_06_page_gone_is_flagged(mid: SourceEntry) -> None:
    r = check_source(mid, fetcher(serve("gone", 404)), stored)
    assert r.status == "failed"
    assert "HTTP 404" in r.problems[0]


def test_EV_6_06_redirect_to_another_page_is_flagged(mid: SourceEntry) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("hdfc-mid-cap-fund-direct-growth"):
            return httpx.Response(301, headers={"Location": "/mutual-funds/search?q=hdfc"})
        return httpx.Response(200, text="<html>search</html>")

    r = check_source(mid, fetcher(handler), stored)
    assert r.status == "failed"
    assert "redirected" in r.problems[0]


def test_EV_6_06_wrong_scheme_is_flagged(mid: SourceEntry) -> None:
    other = fixture_html("groww-hdfc-small-cap")  # the small cap page at the mid cap URL
    r = check_source(mid, fetcher(serve(other)), stored)
    assert r.status == "failed"
    assert "wrong or missing scheme data" in r.problems[0]


def test_bot_check_page_is_flagged(mid: SourceEntry) -> None:
    r = check_source(mid, fetcher(serve("<html>Please verify you are human</html>")), stored)
    assert r.status == "failed"


def test_displayed_value_matching_either_figure_is_fine(mid: SourceEntry) -> None:
    html = fixture_html(mid.id).replace(">0.74%<", ">0.60%<")  # the base figure shown
    assert displayed_expense_ratio(html).value == "0.60%"
    r = check_source(mid, fetcher(serve(html)), stored)
    assert r.status == "ok"


def test_mismatch_warns_with_values_only(
    mid: SourceEntry, caplog: pytest.LogCaptureFixture
) -> None:
    html = fixture_html(mid.id).replace(">0.74%<", ">0.99%<")
    with caplog.at_level(logging.WARNING):
        r = check_source(mid, fetcher(serve(html)), stored)
    assert r.status == "warning"  # a warning, never a hard failure
    assert "0.99%" in r.warnings[0] and "0.60%" in r.warnings[0] and "0.74%" in r.warnings[0]
    line = next(rec.getMessage() for rec in caplog.records if "mismatch" in rec.getMessage())
    assert "scheme=hdfc-mid-cap" in line and "gst_label=False" in line
    assert "<" not in line  # no page HTML in the log


def test_box_not_found_is_its_own_warning(mid: SourceEntry) -> None:
    html = page_with_fund(fund_json(mid.id))  # valid fund JSON, no rendered box
    assert displayed_expense_ratio(html).value is None
    r = check_source(mid, fetcher(serve(html)), stored)
    assert r.status == "warning"
    assert r.warnings == ["expense ratio box not found in the rendered page"]


def test_gst_label_detected() -> None:
    html = re.sub(
        r"(>0\.74%<)", r"\1/div><div>Exclusive of GST &amp; statutory charges<", fixture_html(
            "groww-hdfc-mid-cap"
        ), count=1
    )  # fmt: skip
    assert displayed_expense_ratio(html).gst_label


def test_run_reads_stored_figures(manifest: Manifest, indexed: sessionmaker[Session]) -> None:
    base, total = stored_expense_ratios(indexed, "hdfc-mid-cap")
    assert base and total
    pages = {s.url: fixture_html(s.id) for s in manifest.sources}

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=pages[str(req.url)])

    results = run_linkcheck(manifest, fetcher(handler), indexed)
    assert [r.status for r in results] == ["ok"] * 5
