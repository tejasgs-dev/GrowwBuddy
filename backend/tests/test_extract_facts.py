"""EV-2-07 to EV-2-10: fact extraction (tasks 2.7, 2.8)."""

from __future__ import annotations

from datetime import date

import pytest

from growwbuddy.ingest.extract_facts import (
    CORE_FACT_KEYS,
    ExtractedFact,
    extract_facts,
    grounding_error,
    rule_exit_load,
)
from growwbuddy.ingest.parse_groww import Fact, ParsedPage
from growwbuddy.llm.client import JsonResult
from tests.fakes import FailIfCalledLLM, FakeLLM

EXPECTED = {
    "hdfc-mid-cap": {
        "expense_ratio_direct": "0.74%",
        "expense_ratio_base": "0.60%",
        "min_sip": "₹100",
        "min_lumpsum": "₹100",
        "benchmark": "NIFTY Midcap 150 Total Return Index",
        "riskometer": "Very High",
        "fund_manager": "Chirag Setalvad, Dhruv Muchhal",
        "exit_load": "Exit load of 1% if redeemed within 1 year",
    },
    "hdfc-small-cap": {
        "expense_ratio_direct": "0.79%",
        "expense_ratio_base": "0.64%",
        "benchmark": "BSE 250 SmallCap Total Return Index",
        "fund_manager": "Dhruv Muchhal, Chirag Setalvad",
    },
    "hdfc-large-cap": {
        "expense_ratio_direct": "1.00%",
        "expense_ratio_base": "0.82%",
        "benchmark": "NIFTY 100 Total Return Index",
        "fund_manager": "Rahul Baijal, Dhruv Muchhal",
    },
    "hdfc-silver-etf-fof": {
        "expense_ratio_direct": "0.21%",
        "expense_ratio_base": "0.18%",  # quoted as the page states it (EC-FCT-05)
        "benchmark": "Domestic Price of Silver",
        "exit_load": "Exit load of 1%, if redeemed within 15 days",
        "fund_manager": "Arun Agarwal, Nandita Menezes",
    },
    "hdfc-multi-cap": {
        "expense_ratio_direct": "0.95%",
        "expense_ratio_base": "0.70%",
        "benchmark": "Nifty 500 Multicap 50:25:25 Total Return Index",
        "fund_manager": "Amar Kalkundrikar, Dhruv Muchhal",
    },
}


def _by_key(facts: list[ExtractedFact]) -> dict[str, ExtractedFact]:
    return {f.fact_key: f for f in facts}


def _page(pages: list[ParsedPage], scheme: str) -> ParsedPage:
    return next(p for p in pages if p.scheme_key == scheme)


def _with_field(page: ParsedPage, name: str, value: str) -> ParsedPage:
    fields = dict(page.fields)
    fields[name] = Fact(value=value, as_on=fields[name].as_on if name in fields else None)
    return page.model_copy(update={"fields": fields})


@pytest.mark.parametrize("scheme", sorted(EXPECTED))
def test_EV_2_07_rule_extractors(parsed_pages: list[ParsedPage], scheme: str) -> None:
    facts = _by_key(extract_facts(_page(parsed_pages, scheme), FailIfCalledLLM()))
    for key, value in EXPECTED[scheme].items():
        assert facts[key].value == value, key
        assert facts[key].extracted_by == "rule"
    for key in CORE_FACT_KEYS:
        assert facts[key].verified, (key, facts[key].check_error)
    assert "lock_in" not in facts  # not stated on any page (EC-FCT-08)
    assert facts["expense_ratio_direct"].as_of_date == date(2026, 11, 10)
    assert facts["expense_ratio_base"].as_of_date == date(2026, 11, 10)
    assert facts["benchmark"].as_of_date == date(2026, 11, 11)  # page date
    assert facts["min_sip"].evidence and "SIP" in facts["min_sip"].evidence


@pytest.mark.parametrize(
    ("text", "value"),
    [
        ("Exit load of 1% if redeemed within 1 year.", "Exit load of 1% if redeemed within 1 year"),
        (
            "Exit load of 1%, if redeemed within 15 days.",
            "Exit load of 1%, if redeemed within 15 days",
        ),
        ("Nil", "Nil"),
        ("1% if redeemed within 12 months", "1% if redeemed within 12 months"),
        ("1% if redeemed within 1 year; Nil after 1 year", None),  # multi-tier -> LLM
    ],
)
def test_rule_exit_load(text: str, value: str | None) -> None:
    assert rule_exit_load(text) == value


TIERED = (
    "1% if redeemed within 365 days; 0.5% if redeemed after 365 days but within 730 days; "
    "Nil thereafter."
)


def test_llm_exit_load_used_when_rules_fail(parsed_pages: list[ParsedPage]) -> None:
    page = _with_field(_page(parsed_pages, "hdfc-mid-cap"), "exit_load", TIERED)
    llm = FakeLLM(JsonResult(True, data={"stated": True, "exit_load": TIERED}))
    fact = _by_key(extract_facts(page, llm))["exit_load"]
    assert fact.extracted_by == "llm"
    assert fact.value == TIERED  # full wording kept (EC-FCT-01)
    assert fact.verified
    assert len(llm.calls) == 1 and TIERED in llm.calls[0]["user"]


def test_EV_2_08_llm_number_not_on_page_is_rejected(parsed_pages: list[ParsedPage]) -> None:
    page = _with_field(_page(parsed_pages, "hdfc-mid-cap"), "exit_load", TIERED)
    invented = TIERED.replace("0.5%", "0.25%")
    llm = FakeLLM(JsonResult(True, data={"stated": True, "exit_load": invented}))
    fact = _by_key(extract_facts(page, llm))["exit_load"]
    assert fact.value is None
    assert not fact.verified
    assert fact.check_error and "not in the source" in fact.check_error


def test_grounding_error() -> None:
    assert grounding_error("1% within 365 days", TIERED) is None
    assert grounding_error("2% within 365 days", TIERED) is not None


@pytest.mark.parametrize(
    "result",
    [
        JsonResult(False, error="finish_reason=length", finish_reason="length"),  # max tokens
        JsonResult(False, error="model refused", finish_reason="stop"),  # refusal
        JsonResult(False, error="api error: APIConnectionError"),
        JsonResult(True, data={"stated": False, "exit_load": ""}),
        JsonResult(True, data={"wrong": "shape"}),
    ],
)
def test_EV_2_09_llm_failures_store_nothing_and_flag(
    parsed_pages: list[ParsedPage], result: JsonResult
) -> None:
    page = _with_field(_page(parsed_pages, "hdfc-mid-cap"), "exit_load", TIERED)
    fact = _by_key(extract_facts(page, FakeLLM(result)))["exit_load"]
    assert fact.value is None
    assert not fact.verified
    assert fact.check_error and "review" in fact.check_error


def test_no_llm_configured_flags_exit_load(parsed_pages: list[ParsedPage]) -> None:
    page = _with_field(_page(parsed_pages, "hdfc-mid-cap"), "exit_load", TIERED)
    fact = _by_key(extract_facts(page, None))["exit_load"]
    assert fact.value is None and not fact.verified


def test_EV_2_10_conflicting_ter_is_unverified(parsed_pages: list[ParsedPage]) -> None:
    page = _with_field(_page(parsed_pages, "hdfc-mid-cap"), "expense_ratio_history", "0.77%")
    fact = _by_key(extract_facts(page, FailIfCalledLLM()))["expense_ratio_direct"]
    assert fact.value == "0.74%"
    assert not fact.verified
    assert fact.check_error and "0.74%" in fact.check_error and "0.77%" in fact.check_error
    assert fact.evidence and "0.74%" in fact.evidence and "0.77%" in fact.evidence


@pytest.mark.parametrize("scheme", sorted(EXPECTED))
def test_both_ter_figures_are_extracted(parsed_pages: list[ParsedPage], scheme: str) -> None:
    """Groww has shown either figure on the page, so both must be stored and verified."""
    page = _page(parsed_pages, scheme)
    assert {"expense_ratio", "expense_ratio_base"} <= set(page.fields)
    facts = _by_key(extract_facts(page, FailIfCalledLLM()))
    total, base = facts["expense_ratio_direct"], facts["expense_ratio_base"]
    assert total.verified and base.verified
    assert total.value != base.value
    assert total.evidence and base.evidence and total.evidence == base.evidence
    for v in (total.value, base.value):
        assert v and v in total.evidence  # the chunk text states both figures


def test_conflicting_base_ter_is_unverified(parsed_pages: list[ParsedPage]) -> None:
    page = _with_field(_page(parsed_pages, "hdfc-mid-cap"), "expense_ratio_base_history", "0.55%")
    facts = _by_key(extract_facts(page, FailIfCalledLLM()))
    assert not facts["expense_ratio_base"].verified
    err = facts["expense_ratio_base"].check_error
    assert err and "0.60%" in err and "0.55%" in err
    assert facts["expense_ratio_direct"].verified  # only the disagreeing figure


def test_base_ter_above_total_flags_both(parsed_pages: list[ParsedPage]) -> None:
    page = _page(parsed_pages, "hdfc-mid-cap")
    page = _with_field(page, "expense_ratio_base", "0.90%")
    page = _with_field(page, "expense_ratio_base_history", "0.90%")
    facts = _by_key(extract_facts(page, FailIfCalledLLM()))
    for key in ("expense_ratio_direct", "expense_ratio_base"):
        err = facts[key].check_error
        assert err and "above total TER" in err, key
    assert facts["exit_load"].verified


def test_failed_sanity_check_is_unverified(parsed_pages: list[ParsedPage]) -> None:
    page = _with_field(_page(parsed_pages, "hdfc-mid-cap"), "riskometer", "Medium")
    page = _with_field(page, "expense_ratio", "3.50%")
    page = _with_field(page, "expense_ratio_history", "3.50%")
    facts = _by_key(extract_facts(page, FailIfCalledLLM()))
    assert not facts["riskometer"].verified
    assert not facts["expense_ratio_direct"].verified
    assert facts["min_sip"].verified


def test_missing_field_gives_no_fact(parsed_pages: list[ParsedPage]) -> None:
    page = _page(parsed_pages, "hdfc-mid-cap")
    fields = {k: v for k, v in page.fields.items() if k != "benchmark"}
    facts = _by_key(extract_facts(page.model_copy(update={"fields": fields}), FailIfCalledLLM()))
    assert "benchmark" not in facts
