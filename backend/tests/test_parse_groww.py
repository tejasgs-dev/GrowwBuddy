"""EV-1-03, 04, 09, 10, 14, 15: parsing saved Groww pages.

Snapshot tests: a Groww layout or data change makes these fail. After checking the new
output by hand against the live page, refresh snapshots with:

    UPDATE_SNAPSHOTS=1 python -m pytest tests/test_parse_groww.py
"""

from __future__ import annotations

import json
import os
from datetime import date

import pytest

from growwbuddy.constants import RISKOMETER_LEVELS, SOURCES
from growwbuddy.ingest.manifest import Manifest
from growwbuddy.ingest.parse_groww import (
    CORE_FIELDS,
    ParseError,
    content_hash,
    extract_fund_data,
    parse_page,
)
from tests.conftest import FIXED_NOW, SNAPSHOTS, fixture_html, fund_json, page_with_fund

SOURCE_IDS = [s[0] for s in SOURCES]


@pytest.mark.parametrize("source_id", SOURCE_IDS)
def test_EV_1_09_snapshot(manifest: Manifest, source_id: str) -> None:
    page = parse_page(fixture_html(source_id), manifest.get(source_id), FIXED_NOW)
    actual = page.model_dump(mode="json")
    snap = SNAPSHOTS / f"{source_id}.json"
    if os.environ.get("UPDATE_SNAPSHOTS") == "1":
        snap.parent.mkdir(parents=True, exist_ok=True)
        snap.write_text(json.dumps(actual, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    assert snap.exists(), f"missing snapshot {snap.name}; run with UPDATE_SNAPSHOTS=1"
    assert actual == json.loads(snap.read_text(encoding="utf-8"))


@pytest.mark.parametrize("source_id", SOURCE_IDS)
def test_EV_1_14_core_fields_present(manifest: Manifest, source_id: str) -> None:
    page = parse_page(fixture_html(source_id), manifest.get(source_id), FIXED_NOW)
    for field in CORE_FIELDS:
        assert field in page.fields, field
    assert page.fields["riskometer"].value in RISKOMETER_LEVELS
    assert page.fields["expense_ratio"].value.endswith("%")
    assert page.fields["min_sip"].value.startswith("₹")


@pytest.mark.parametrize("source_id", SOURCE_IDS)
def test_EV_1_15_as_of_date_from_page(manifest: Manifest, source_id: str) -> None:
    page = parse_page(fixture_html(source_id), manifest.get(source_id), FIXED_NOW)
    assert page.as_of_origin == "page"
    assert page.as_of_date == date(2026, 11, 11)  # NAV date on the saved pages


def test_riskometer_uses_current_value_not_nfo(manifest: Manifest) -> None:
    # On the saved Mid Cap page, nfo_risk is the stale "Moderately High"; the page shows
    # "Very High".
    fund = fund_json("groww-hdfc-mid-cap")
    assert fund["nfo_risk"] == "Moderately High"
    page = parse_page(
        fixture_html("groww-hdfc-mid-cap"), manifest.get("groww-hdfc-mid-cap"), FIXED_NOW
    )
    assert page.fields["riskometer"].value == "Very High"


def test_fund_managers_use_current_team(manifest: Manifest) -> None:
    fund = fund_json("groww-hdfc-large-cap")
    page = parse_page(page_with_fund(fund), manifest.get("groww-hdfc-large-cap"), FIXED_NOW)
    assert fund["fund_manager"] not in page.fields["fund_managers"].value


@pytest.mark.parametrize("source_id", SOURCE_IDS)
def test_EV_1_10_unwanted_content_dropped(manifest: Manifest, source_id: str) -> None:
    fund = fund_json(source_id)
    page = parse_page(fixture_html(source_id), manifest.get(source_id), FIXED_NOW)
    dumped = page.model_dump_json()

    # Groww's peer list and "funds managed" lists include this scheme itself; skip it.
    peer_names = [p["scheme_name"] for p in fund.get("peerComparison") or []]
    other_managed = [
        f["scheme_name"]
        for m in fund.get("fund_manager_details") or []
        for f in m.get("funds_managed") or []
    ]
    holdings = [h.get("company_name") for h in fund.get("holdings") or [] if h.get("company_name")]
    others = {n for n in [*peer_names, *other_managed, *holdings] if n != page.scheme_name}
    assert others  # the fixture really does contain other schemes and companies
    for name in others:
        assert name not in dumped, name

    for banned in ("return1y", "return3y", "CAGR", "sharpe", "NAV:", "groww_rating"):
        assert banned not in dumped
    assert str(fund["nav"]) not in dumped  # NAV value is performance data (EC-INT-06)
    assert fund["amc_info"]["email"] not in dumped


def test_EV_1_04_bot_check_page_rejected(manifest: Manifest) -> None:
    html = "<html><body><h1>Access denied</h1><div class='captcha'></div></body></html>"
    with pytest.raises(ParseError, match="no embedded fund data"):
        parse_page(html, manifest.get("groww-hdfc-mid-cap"), FIXED_NOW)


def test_malformed_json_rejected(manifest: Manifest) -> None:
    html = '<script id="__NEXT_DATA__" type="application/json">{not json</script>'
    with pytest.raises(ParseError, match="unexpected shape"):
        parse_page(html, manifest.get("groww-hdfc-mid-cap"), FIXED_NOW)


def test_EV_1_03_wrong_scheme_rejected(manifest: Manifest) -> None:
    # The small cap page served at the mid cap URL (e.g. after a redirect).
    with pytest.raises(ParseError, match="expected 'hdfc-mid-cap-fund-direct-growth'"):
        parse_page(
            fixture_html("groww-hdfc-small-cap"), manifest.get("groww-hdfc-mid-cap"), FIXED_NOW
        )


def test_regular_plan_rejected(manifest: Manifest) -> None:
    fund = fund_json("groww-hdfc-mid-cap")
    fund["plan_type"] = "Regular"
    with pytest.raises(ParseError, match="Direct - Growth"):
        parse_page(page_with_fund(fund), manifest.get("groww-hdfc-mid-cap"), FIXED_NOW)


def test_placeholders_become_missing(manifest: Manifest) -> None:
    fund = fund_json("groww-hdfc-mid-cap")
    fund["expense_ratio"] = "0"
    fund["base_expense_ratio"] = "-"
    fund["benchmark_name"] = "NA"
    fund["exit_load"] = "--"
    page = parse_page(page_with_fund(fund), manifest.get("groww-hdfc-mid-cap"), FIXED_NOW)
    for field in ("expense_ratio", "expense_ratio_base", "benchmark", "exit_load"):
        assert field not in page.fields
        assert field in page.missing
    assert "expense_ratio" not in {s.name for s in page.sections}


@pytest.mark.parametrize(
    ("total", "base", "expected"),
    [
        ("0", "0.60", "is 0.60% excluding GST and statutory charges, as on"),
        ("0.74", "NA", "is 0.74% including GST and statutory charges, as on"),
    ],
)
def test_one_ter_figure_is_labelled(
    manifest: Manifest, total: str, base: str, expected: str
) -> None:
    """With only one figure, the section still says which one it is."""
    fund = fund_json("groww-hdfc-mid-cap")
    fund["expense_ratio"], fund["base_expense_ratio"] = total, base
    page = parse_page(page_with_fund(fund), manifest.get("groww-hdfc-mid-cap"), FIXED_NOW)
    sec = next(s for s in page.sections if s.name == "expense_ratio")
    assert expected in sec.text
    assert ("expense_ratio" in page.missing) == (total == "0")
    assert ("expense_ratio_base" in page.missing) == (base == "NA")


def test_nil_exit_load_is_a_value(manifest: Manifest) -> None:
    fund = fund_json("groww-hdfc-mid-cap")
    fund["exit_load"] = "Nil"
    page = parse_page(page_with_fund(fund), manifest.get("groww-hdfc-mid-cap"), FIXED_NOW)
    assert page.fields["exit_load"].value == "Nil"


def test_lock_in_only_when_stated(manifest: Manifest) -> None:
    fund = fund_json("groww-hdfc-mid-cap")
    page = parse_page(page_with_fund(fund), manifest.get("groww-hdfc-mid-cap"), FIXED_NOW)
    assert "lock_in" not in page.fields
    fund["lock_in"] = {"years": 3, "months": None, "days": None}
    page = parse_page(page_with_fund(fund), manifest.get("groww-hdfc-mid-cap"), FIXED_NOW)
    assert page.fields["lock_in"].value == "3 years"


def test_hash_ignores_dropped_content(manifest: Manifest) -> None:
    """EC-ING-09: returns/NAV/ads changing must not change the content hash."""
    source = manifest.get("groww-hdfc-mid-cap")
    fund = fund_json("groww-hdfc-mid-cap")
    h1 = content_hash(extract_fund_data(page_with_fund(fund), source))
    fund["nav"] = 999.99
    fund["return_stats"][0]["return1y"] = 99.9
    fund["peerComparison"] = []
    h2 = content_hash(extract_fund_data(page_with_fund(fund), source))
    assert h1 == h2
    fund["expense_ratio"] = "0.80"
    h3 = content_hash(extract_fund_data(page_with_fund(fund), source))
    assert h3 != h1


def test_sections_name_the_scheme(manifest: Manifest) -> None:
    page = parse_page(
        fixture_html("groww-hdfc-silver-etf-fof"),
        manifest.get("groww-hdfc-silver-etf-fof"),
        FIXED_NOW,
    )
    assert page.sections
    for s in page.sections:
        assert page.scheme_name in s.text
        assert s.text.endswith(".")
