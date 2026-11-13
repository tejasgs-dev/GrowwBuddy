"""Output validator (task 3.10; EV-3-09 to 15, 19)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from growwbuddy.ingest.parse_groww import ParsedPage
from growwbuddy.llm.schemas import Answer
from growwbuddy.pipeline.types import Citation, Response
from growwbuddy.pipeline.validator import (
    check_response,
    count_sentences,
    has_return_figure,
    quantities_in,
    split_sentences,
    ungrounded_quantities,
    validate_answer,
)
from growwbuddy.retrieval.headers import RetrievalHeaders
from growwbuddy.retrieval.retriever import ContextItem, Retrieval

MID_URL = "https://groww.in/mutual-funds/hdfc-mid-cap-fund-direct-growth"
TER_TEXT = "The expense ratio (TER) of HDFC Mid Cap Fund Direct Growth is 0.74% as on 10 Nov 2026."
EL_TEXT = "Exit load of HDFC Mid Cap Fund Direct Growth: Exit load of 1% if redeemed within 1 year."


def item(item_id: str, text: str, scheme: str = "hdfc-mid-cap", url: str = MID_URL) -> ContextItem:
    return ContextItem(
        item_id=item_id, kind="chunk", scheme=scheme, text=text, source_url=url,
        as_of_date=date(2026, 11, 11), fact_as_on=None, fetched_at=None,
    )  # fmt: skip


RETRIEVAL = Retrieval(
    items=[
        item("groww-hdfc-mid-cap#expense_ratio", TER_TEXT),
        item("groww-hdfc-mid-cap#exit_load", EL_TEXT),
        item(
            "groww-hdfc-small-cap#exit_load",
            EL_TEXT.replace("Mid", "Small"),
            scheme="hdfc-small-cap",
        ),
        item("bad#url", TER_TEXT, url="https://example.com/fund"),
    ]
)


def ans(*sentences: str, cite: str = "groww-hdfc-mid-cap#expense_ratio") -> Answer:
    return Answer(answerable=True, sentences=sentences, source_chunk_id=cite or None)


def check(a: Answer, h: RetrievalHeaders, scheme: str = "hdfc-mid-cap") -> tuple[str, str]:
    r = validate_answer(a, RETRIEVAL, scheme, h)
    return r.action, " ".join(r.errors)


GOOD = "The expense ratio (TER) of HDFC Mid Cap Fund Direct Growth is 0.74% as on 10 Nov 2026."


def test_good_answer_passes(retrieval_headers: RetrievalHeaders) -> None:
    assert check(ans(GOOD), retrieval_headers) == ("ok", "")


# ---------------------------------------------------------------- EV-3-09 sentences


@pytest.mark.parametrize(
    ("text", "n"),
    [
        ("The minimum SIP is Rs. 500 per month.", 1),
        ("Charges apply, e.g. stamp duty of 0.005%.", 1),
        ("The registrar is Computer Age Management Services Ltd. in Chennai.", 1),
        ("The TER is 0.74%. The exit load is 1%.", 2),
        ("One. Two. Three.", 3),
        ("One. Two. Three. Four.", 4),
        ("- one\n- two\n- three\n- four", 4),
        ("1. one\n2. two", 2),
        ("It is 1.5 years! Really? Yes.", 3),
    ],
)
def test_EV_3_09_sentence_splitter(text: str, n: int) -> None:
    assert len(split_sentences(text)) == n


def test_EV_3_09_count_limits(retrieval_headers: RetrievalHeaders) -> None:
    three = ("The TER is 0.74%.", "It is as on 10 Nov 2026.", "It is for the Direct plan.")
    assert check(ans(*three), retrieval_headers)[0] == "ok"
    # Model packed four sentences into three items.
    four = (*three[:2], "It is for the Direct plan. It is Growth.")
    action, errors = check(ans(*four), retrieval_headers)
    assert action == "retry" and "4 sentences" in errors
    assert count_sentences(["- a\n- b\n- c\n- d"]) == 4  # a 4-item list fails (EC-VAL-03)


# ---------------------------------------------------------------- EV-3-10 citation


@pytest.mark.parametrize(
    "cite",
    [
        "",
        "groww-hdfc-mid-cap#nope",
        "groww-hdfc-mid-cap#expense_ratio, groww-hdfc-mid-cap#exit_load",
    ],
)
def test_EV_3_10_citation_must_be_one_known_id(
    cite: str, retrieval_headers: RetrievalHeaders
) -> None:
    assert check(ans(GOOD, cite=cite), retrieval_headers)[0] == "retry"


def test_EV_3_10_non_groww_url_fails(retrieval_headers: RetrievalHeaders) -> None:
    action, errors = check(ans(GOOD, cite="bad#url"), retrieval_headers)
    assert action == "retry" and "domain" in errors


# ---------------------------------------------------------------- EV-3-11 URLs


@pytest.mark.parametrize(
    "extra",
    ["See https://groww.in/x.", "See groww.in/mutual-funds.", "Visit www.hdfcfund.com."],
)
def test_EV_3_11_no_urls(extra: str, retrieval_headers: RetrievalHeaders) -> None:
    action, errors = check(ans(GOOD, extra), retrieval_headers)
    assert action == "retry" and "URL" in errors


# ---------------------------------------------------------------- EV-3-12 advice


@pytest.mark.parametrize(
    "extra",
    [
        "We recommend this fund.",
        "You should invest early.",
        "It is a good choice.",
        "It is better than its peers.",
        "It may outperform the index.",
        "This low cost makes it attractive.",
        "Click here to invest now.",
    ],
)
def test_EV_3_12_advice_blocked(extra: str, retrieval_headers: RetrievalHeaders) -> None:
    assert check(ans(GOOD, extra), retrieval_headers)[0] == "advice"


# ---------------------------------------------------------------- EV-3-13 return figures


@pytest.mark.parametrize(
    "text",
    [
        "HDFC Mid Cap Fund returned 18% last year.",
        "It has a CAGR of 21%.",
        "The TER gained 0.02% this month.",
        "Its NAV is ₹123.45.",
    ],
)
def test_EV_3_13_return_figures_blocked(text: str, retrieval_headers: RetrievalHeaders) -> None:
    assert has_return_figure(text)
    assert check(ans(text), retrieval_headers)[0] == "performance"


@pytest.mark.parametrize(
    "text",
    [
        "Exit load of 1% if redeemed within 1 year.",
        "If you redeem within one year, returns are taxed at 20%.",  # quirk 4
        "The expense ratio is 0.74%.",
    ],
)
def test_EV_3_13_allowed_percentages(text: str) -> None:
    assert not has_return_figure(text)


# ---------------------------------------------------------------- EV-3-14, 15


def test_EV_3_14_numbers_must_be_in_the_cited_item(retrieval_headers: RetrievalHeaders) -> None:
    bad = "The expense ratio of HDFC Mid Cap Fund Direct Growth is 0.47%."
    action, errors = check(ans(bad), retrieval_headers)
    assert action == "retry" and "numbers" in errors


def test_quantities_in_reads_units_and_values() -> None:
    q = quantities_in("TER 1.00% as on 10 Nov 2026; SIP ₹1,000; Rs 1.25 lakh; 01 Jan")
    assert ("percent", Decimal("1")) in q
    assert ("plain", Decimal("10")) in q and ("plain", Decimal("2026")) in q
    assert ("rupee", Decimal("1000")) in q  # Indian grouping comma
    assert ("rupee", Decimal("125000")) in q  # lakh scaled
    assert ("plain", Decimal("1")) in q  # "01" is 1
    assert quantities_in("0.74%.") == {("percent", Decimal("0.74"))}  # full stop isn't a decimal


LC_TER = "The expense ratio (TER) of HDFC Large Cap Fund Direct Growth is 1.00% as on 10 Nov 2026."


@pytest.mark.parametrize(
    ("answer", "source", "grounded"),
    [
        # Same value, different text: all match "1.00%".
        ("The TER is 1%.", LC_TER, True),
        ("The TER is 1.0%.", LC_TER, True),
        ("The TER is 1.00%.", LC_TER, True),
        ("The TER is 1 per cent.", LC_TER, True),
        # Different value still fails.
        ("The TER is 0.47%.", TER_TEXT, False),
        ("The TER is 1.10%.", LC_TER, False),
        ("The TER is 0.1%.", LC_TER, False),
        # Units must agree: a percentage never matches a rupee amount, or the reverse.
        ("The minimum SIP is 100%.", "The minimum SIP amount is ₹100.", False),
        ("The TER is ₹1.", LC_TER, False),
        ("The exit load is 1%.", "Exit load applies if redeemed within 1 year.", False),
        # Rupee spellings and Indian scales.
        ("The minimum SIP is Rs. 100.", "The minimum SIP amount is ₹100.", True),
        ("The minimum SIP is 100 rupees.", "The minimum SIP amount is ₹100.", True),
        ("The minimum SIP is INR 100.", "The minimum SIP amount is ₹100.", True),
        (
            "Gains above ₹1,25,000 are taxed at 12.5%.",
            "exceeding Rs 1.25 lakh are taxed at 12.5%",
            True,
        ),
        (
            "Gains above ₹1,25,000 are taxed at 12.5%.",
            "exceeding Rs 1.5 lakh are taxed at 12.5%",
            False,
        ),
        # A bare number may drop its unit word, but not change its value.
        ("It is 1.00 percent as on 10 Nov 2026.", LC_TER, True),
        ("The minimum SIP is 100.", "The minimum SIP amount is ₹100.", True),
        ("It was launched on 1 Jan 2013.", "The launch date is 01 Jan 2013.", True),
        ("As on 11 Nov 2026.", LC_TER, False),
    ],
)
def test_EV_3_14_numbers_compared_by_value_and_unit(
    answer: str, source: str, grounded: bool
) -> None:
    assert (ungrounded_quantities(answer, source) == []) is grounded


@pytest.mark.parametrize(
    ("answer", "ok"),
    [
        (
            "The expense ratio (TER) of HDFC Large Cap Fund Direct Growth is 1% as on 10 Nov 2026.",
            True,
        ),
        ("The expense ratio of HDFC Large Cap Fund Direct Growth is 1.0% as on 10 Nov 2026.", True),
        (
            "The expense ratio of HDFC Large Cap Fund Direct Growth is 1.00% as on 10 Nov 2026.",
            True,
        ),
        (
            "The expense ratio of HDFC Large Cap Fund Direct Growth is 1.01% as on 10 Nov 2026.",
            False,
        ),
        ("The expense ratio of HDFC Large Cap Fund Direct Growth is ₹1 as on 10 Nov 2026.", False),
    ],
)
def test_large_cap_ter_on_the_real_page(
    answer: str,
    ok: bool,
    parsed_pages: list[ParsedPage],
    retrieval_headers: RetrievalHeaders,
) -> None:
    """Large cap is the scheme whose TER the page writes with trailing zeros (1.00%)."""
    page = next(p for p in parsed_pages if p.scheme_key == "hdfc-large-cap")
    sec = next(s for s in page.sections if s.name == "expense_ratio")
    assert "1.00%" in sec.text and "0.82%" in sec.text
    cid = f"{page.source_id}#expense_ratio"
    r = Retrieval(items=[item(cid, sec.text, scheme="hdfc-large-cap", url=page.url)])
    a = Answer(answerable=True, sentences=(answer,), source_chunk_id=cid)
    assert validate_answer(a, r, "hdfc-large-cap", retrieval_headers).ok is ok


@pytest.mark.parametrize(
    ("answer", "ok"),
    [
        (
            "The expense ratio of HDFC Small Cap Fund Direct Growth is 0.64% excluding GST and "
            "statutory charges, and 0.79% including them, as on 10 Nov 2026.",
            True,
        ),
        ("HDFC Small Cap Fund Direct Growth has an expense ratio of 0.64%.", True),
        ("HDFC Small Cap Fund Direct Growth has an expense ratio of 0.79%.", True),
        ("HDFC Small Cap Fund Direct Growth has an expense ratio of 0.65%.", False),
    ],
)
def test_answer_quoting_both_ter_figures(
    answer: str,
    ok: bool,
    parsed_pages: list[ParsedPage],
    retrieval_headers: RetrievalHeaders,
) -> None:
    """The chunk states both figures, so an answer quoting both passes the number check."""
    page = next(p for p in parsed_pages if p.scheme_key == "hdfc-small-cap")
    sec = next(s for s in page.sections if s.name == "expense_ratio")
    cid = f"{page.source_id}#expense_ratio"
    r = Retrieval(items=[item(cid, sec.text, scheme="hdfc-small-cap", url=page.url)])
    a = Answer(answerable=True, sentences=(answer,), source_chunk_id=cid)
    assert validate_answer(a, r, "hdfc-small-cap", retrieval_headers).ok is ok


def test_EV_3_15_cited_scheme_must_match(retrieval_headers: RetrievalHeaders) -> None:
    text = "HDFC Small Cap Fund Direct Growth has an exit load of 1% if redeemed within 1 year."
    action, errors = check(ans(text, cite="groww-hdfc-small-cap#exit_load"), retrieval_headers)
    assert action == "retry" and "different scheme" in errors


def test_EV_3_15_answer_must_not_name_another_scheme(
    retrieval_headers: RetrievalHeaders,
) -> None:
    text = "HDFC Small Cap Fund has an expense ratio of 0.74% as on 10 Nov 2026."
    action, errors = check(ans(text), retrieval_headers)
    assert action == "retry" and "other than" in errors


def test_every_real_section_passes_as_an_answer(
    parsed_pages: list[ParsedPage], retrieval_headers: RetrievalHeaders
) -> None:
    """Quoting any section verbatim must pass: no false advice/return/URL/scheme hits.

    The multi cap objective names "large cap, mid cap and small cap" and the tax text says
    "returns are taxed at 20%"; both must pass.
    """
    for page in parsed_pages:
        for sec in page.sections:
            cid = f"{page.source_id}#{sec.name}"
            r = Retrieval(items=[item(cid, sec.text, scheme=page.scheme_key, url=page.url)])
            a = Answer(answerable=True, sentences=(sec.text,), source_chunk_id=cid)
            result = validate_answer(a, r, page.scheme_key, retrieval_headers)
            assert result.ok, (cid, result.errors)


# ---------------------------------------------------------------- EV-3-19 response gate


def _resp(type_: str, link: bool, scheme: str | None = "hdfc-mid-cap") -> Response:
    return Response(
        type=type_,  # type: ignore[arg-type]
        text="Some fixed text.",
        citation=Citation(MID_URL, "t") if link else None,
        footer="Last updated from sources: 11 Nov 2026" if link else None,
        scheme=scheme,
    )


@pytest.mark.parametrize(
    "type_", ["pii_rejected", "greeting", "clarification", "unavailable", "busy"]
)
def test_EV_3_19_zero_link_types(type_: str) -> None:
    assert check_response(_resp(type_, link=False)) == []
    assert check_response(_resp(type_, link=True)) != []


@pytest.mark.parametrize("type_", ["answer", "refusal", "performance", "not_found"])
def test_EV_3_19_other_types_need_one_link(type_: str) -> None:
    assert check_response(_resp(type_, link=True)) == []
    assert check_response(_resp(type_, link=False)) != []


@pytest.mark.parametrize("type_", ["refusal", "performance", "not_found"])
def test_linkless_refusal_without_scheme_is_allowed(type_: str) -> None:
    assert check_response(_resp(type_, link=False, scheme=None)) == []  # EC-VAL-07


def test_answer_without_link_is_never_allowed() -> None:
    assert check_response(_resp("answer", link=False, scheme=None)) != []


def test_link_needs_footer_and_allowed_domain() -> None:
    r = Response(type="answer", text="x.", citation=Citation(MID_URL, "t"), scheme="hdfc-mid-cap")
    assert "footer" in " ".join(check_response(r))
    r = Response(
        type="answer", text="x.", citation=Citation("http://groww.in.evil.com/", "t"),
        footer="f", scheme="hdfc-mid-cap",
    )  # fmt: skip
    assert "domain" in " ".join(check_response(r))
