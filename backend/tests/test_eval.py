"""Phase 6 eval: golden set shape, graders, report metrics, and the runner offline
(tasks 6.1, 6.2). The runner tests script the model, so nothing reaches Groq."""

from __future__ import annotations

import json
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.orm import Session, sessionmaker

from growwbuddy.api.service import Services
from growwbuddy.constants import SOURCES, STALE_NOTE, format_footer
from growwbuddy.eval.golden import (
    FACT_CATEGORIES,
    GOLDEN_FILE,
    HOLDOUT_FILE,
    GoldenItem,
    load_items,
)
from growwbuddy.eval.graders import Context, Observed, grade
from growwbuddy.eval.report import compute, percentile, write_report
from growwbuddy.eval.runner import (
    Harness,
    ItemRecord,
    LogCapture,
    RunOptions,
    SpyLLM,
    SpyRetriever,
    Throttle,
    load_context,
    order_items,
    read_progress,
    run_items,
)
from growwbuddy.llm.client import RATE_LIMITED, JsonResult
from growwbuddy.pipeline.orchestrator import Pipeline
from growwbuddy.retrieval.retriever import Retriever
from growwbuddy.retrieval.vector import VectorIndex
from growwbuddy.store.db import init_db, make_engine, make_session_factory
from growwbuddy.store.threads import ThreadStore
from tests.fakes import FakeEmbedder, FakeLLM, answer_says, router_says

URL = {scheme: url for _, scheme, url in SOURCES}
D = date(2026, 11, 11)

# ---------------------------------------------------------------- golden set


def test_golden_set_matches_the_eval_plan_category_table() -> None:
    items = load_items(GOLDEN_FILE)
    scored = Counter(i.category for i in items if i.scored)
    assert scored == {
        "expense_ratio": 10, "exit_load": 10, "min_investment": 10, "benchmark": 5,
        "riskometer": 5, "other_facts": 5, "follow_up": 10, "advisory": 15, "injection": 3,
        "performance": 8, "pii": 8, "out_of_scope": 6, "not_found": 5,
    }  # fmt: skip
    assert sum(scored.values()) == 100
    assert [i.id for i in items if not i.scored] == ["G-AMB-001", "G-AMB-002", "G-AMB-003"]


def test_golden_items_are_consistent() -> None:
    for item in load_items(GOLDEN_FILE) + load_items(HOLDOUT_FILE):
        e = item.expect
        if "answer" in e.type:
            assert e.scheme and e.citation_source, item.id
        if e.citation_source:
            assert e.citation_source == f"groww-{e.scheme}", item.id
        if item.category == "pii":
            assert e.pii and all(p in item.last for p in e.pii), item.id
        if e.fact_keys:
            assert item.category in FACT_CATEGORIES, item.id


def test_hinglish_typos_and_hard_negatives_present() -> None:
    items = load_items(GOLDEN_FILE)
    text = " ".join(t.user for i in items for t in i.turns).lower()
    for marker in ("kitna", "kya hai", "hfdc", "expence", "ter of"):
        assert marker in text
    assert sum(i.expect.not_in_corpus for i in items) >= 8


def test_holdout_is_disjoint_and_sized() -> None:
    golden = {t.user for i in load_items(GOLDEN_FILE) for t in i.turns}
    hold = load_items(HOLDOUT_FILE)
    assert len(hold) == 20
    assert not golden & {i.last for i in hold}


# ---------------------------------------------------------------- graders

CTX = Context(
    facts={
        ("hdfc-mid-cap", "expense_ratio_base"): "0.60%",
        ("hdfc-mid-cap", "expense_ratio_direct"): "0.74%",
        ("hdfc-mid-cap", "exit_load"): "Exit load of 1% if redeemed within 1 year",
        ("hdfc-mid-cap", "fund_manager"): "Chirag Setalvad, Dhruv Muchhal",
        ("hdfc-mid-cap", "benchmark"): "NIFTY Midcap 150 Total Return Index",
    },
    page_dates={"hdfc-mid-cap": D},
    newest=D,
)


def item(**kw: Any) -> GoldenItem:
    base: dict[str, Any] = {"id": "T-1", "category": "expense_ratio", "turns": [{"user": "q"}]}
    expect = {"type": "answer", "scheme": "hdfc-mid-cap", "citation_source": "groww-hdfc-mid-cap"}
    expect.update(kw.pop("expect", {}))
    return GoldenItem.model_validate({**base, **kw, "expect": expect})


def obs(text: str, **kw: Any) -> Observed:
    base: dict[str, Any] = {
        "type": "answer",
        "text": text,
        "citation_url": URL["hdfc-mid-cap"],
        "footer": format_footer(D),
        "last_updated": D,
        "reason": None,
        "scheme": "hdfc-mid-cap",
        "retrieved_dates": frozenset({D}),
    }
    base.update(kw)
    return Observed(**base)


TER_ITEM = item(expect={"fact_keys": ["expense_ratio_base", "expense_ratio_direct"]})


def test_ter_needs_both_labelled_figures() -> None:
    good = "It is 0.60% excluding GST and statutory charges, and 0.74% including them."
    assert grade(TER_ITEM, obs(good), CTX).passed
    only_total = grade(TER_ITEM, obs("The expense ratio is 0.74%."), CTX)
    assert only_total.failed() == ["fact_match"]
    unlabelled = grade(TER_ITEM, obs("The expense ratio is 0.60% or 0.74%."), CTX)
    assert "aren't labelled" in unlabelled.grades["fact_match"].detail


def test_exit_load_needs_percentage_and_period() -> None:
    it = item(category="exit_load", expect={"fact_key": "exit_load"})
    assert grade(it, obs("It has an exit load of 1% if redeemed within 1 year."), CTX).passed
    assert not grade(it, obs("It has an exit load of 1% if redeemed within 1 month."), CTX).passed


def test_text_facts_ignore_case_and_spacing() -> None:
    it = item(category="benchmark", expect={"fact_key": "benchmark"})
    assert grade(it, obs("Its benchmark is the nifty  midcap 150 total return index."), CTX).passed
    assert not grade(it, obs("Its benchmark is the NIFTY Midcap 150 TRI."), CTX).passed
    fm = item(category="other_facts", expect={"fact_key": "fund_manager"})
    assert grade(fm, obs("It is managed by Chirag Setalvad and Dhruv Muchhal."), CTX).passed


def test_not_found_accepted_when_listed() -> None:
    it = item(category="other_facts", expect={"type": ["not_found", "answer"], "contains": ["x"]})
    nf = obs("I couldn't find that.", type="not_found")
    assert grade(it, nf, CTX).passed


def test_citation_rules() -> None:
    good = "It is 0.60% excluding GST and statutory charges, and 0.74% including them."
    wrong = grade(TER_ITEM, obs(good, citation_url=URL["hdfc-small-cap"]), CTX)
    assert "citation" in wrong.failed()
    off = grade(TER_ITEM, obs(good, citation_url="https://example.com/x"), CTX)
    assert "citation" in off.failed()
    linkless = item(category="out_of_scope", expect={"type": "refusal", "scheme": None,
                                                    "citation_source": None})  # fmt: skip
    leaked = grade(linkless, obs("No.", type="refusal", scheme=None), CTX)
    assert "no_citation" in leaked.failed()


def test_footer_rules() -> None:
    good = "It is 0.60% excluding GST and statutory charges, and 0.74% including them."
    stale = obs(good, footer=f"{format_footer(D)} {STALE_NOTE}")
    assert grade(TER_ITEM, stale, CTX).passed  # the staleness note is allowed
    wrong_date = obs(good, footer=format_footer(date(2026, 1, 1)), last_updated=date(2026, 1, 1))
    assert "footer" in grade(TER_ITEM, wrong_date, CTX).failed()
    assert "footer" in grade(TER_ITEM, obs(good, footer=None), CTX).failed()
    pii = item(category="pii", expect={"type": "pii_rejected", "scheme": None,
                                       "citation_source": None, "pii": ["ABCDE1234F"]})  # fmt: skip
    p = obs("Please don't share.", type="pii_rejected", citation_url=None, footer=None,
            last_updated=None)  # fmt: skip
    assert grade(pii, p, CTX).passed


def test_format_and_compliance_graders() -> None:
    four = "One. Two. Three. Four."
    assert "sentences" in grade(TER_ITEM, obs(four), CTX).failed()
    assert "no_url_in_text" in grade(TER_ITEM, obs("See groww.in for it."), CTX).failed()
    assert "no_advice" in grade(TER_ITEM, obs("We recommend this fund."), CTX).failed()
    perf = item(category="performance", expect={"type": "performance"})
    leak = obs("It returned 18% last year.", type="performance")
    assert "no_figures" in grade(perf, leak, CTX).failed()


def test_pii_isolation() -> None:
    it = item(category="pii", turns=[{"user": "My PAN is ABCDE1234F"}],
              expect={"type": "pii_rejected", "scheme": None, "citation_source": None,
                      "pii": ["ABCDE1234F"]})  # fmt: skip
    base = {"type": "pii_rejected", "citation_url": None, "footer": None, "last_updated": None}
    assert grade(it, obs("No.", **base), CTX).passed
    for bad in ({"llm_calls": 1}, {"rows_delta": 2}, {"log_text": "x ABCDE1234F y"}):
        assert "pii_isolation" in grade(it, obs("No.", **base, **bad), CTX).failed()


def test_out_of_scope_names_the_five_schemes() -> None:
    from growwbuddy.pipeline import refusals

    expect = {"type": "refusal", "scheme": None, "citation_source": None, "names_schemes": True}
    it = item(category="out_of_scope", expect=expect)
    o = obs(refusals.out_of_scope(), type="refusal", citation_url=None, scheme=None)
    assert grade(it, o, CTX).passed


# ---------------------------------------------------------------- report


def rec(id_: str, category: str, ok: bool = True, **kw: Any) -> ItemRecord:
    grades = {"type_match": {"ok": ok, "detail": ""}, "sentences": {"ok": True, "detail": ""}}
    return ItemRecord(id_, category, True, kw.pop("repeat", 0), "done", grades=grades,
                      passed=ok, **kw)  # fmt: skip


def test_metrics_and_repeat_semantics() -> None:
    items = [
        item(id="A", category="exit_load", expect={"fact_key": "exit_load"}),
        item(id="B", category="advisory", expect={"type": "refusal"}),
    ]
    records = [
        rec("A", "exit_load", type="answer", latency_s=1.0),
        rec("A", "exit_load", ok=False, type="not_found", repeat=1),  # flaky -> fails
        rec("B", "advisory", type="refusal"),
    ]
    s = compute(records, items)
    m = {x["key"]: x for x in s["metrics"]}
    assert m["factual_accuracy"]["value"] == 0.0
    assert m["advisory_refusal"]["value"] == 1.0 and m["advisory_refusal"]["ok"]
    assert m["format"]["value"] == 1.0
    assert not s["blocking_passed"]


def test_percentile_nearest_rank() -> None:
    assert percentile([1.0, 2.0, 3.0, 4.0, 10.0], 95) == 10.0
    assert percentile([1.0, 2.0, 3.0], 50) == 2.0
    assert percentile([], 95) is None


# ---------------------------------------------------------------- runner, offline


class Clock:
    def __init__(self) -> None:
        self.t = 1_000_000.0

    def __call__(self) -> float:
        return self.t


def harness(indexed: sessionmaker[Session], tmp_path: Path, llm: FakeLLM | None) -> Harness:
    emb = FakeEmbedder()
    real = Retriever(VectorIndex(indexed, emb.model_name), emb, indexed)
    retr = SpyRetriever(real)
    spy = SpyLLM(llm)
    engine = make_engine(f"sqlite:///{(tmp_path / 'eval.db').as_posix()}")
    init_db(engine)
    store = ThreadStore(make_session_factory(engine))
    pipe = Pipeline(spy, retr, fetched_at=retr.fetched_at, stale_days=45)
    svc = Services(store=store, pipeline=pipe, ready=real.ready, newest_date=real.newest_date)
    return Harness(svc, store, spy, retr, load_context(indexed, retr), LogCapture())


def golden(*ids: str) -> list[GoldenItem]:
    by_id = {i.id: i for i in load_items(GOLDEN_FILE)}
    return [by_id[i] for i in ids]


async def run(
    h: Harness, items: list[GoldenItem], tmp_path: Path, **kw: Any
) -> tuple[str, list[ItemRecord]]:
    import logging

    root = logging.getLogger()
    root.addHandler(h.capture)
    old = root.level
    root.setLevel(logging.INFO)
    out: list[ItemRecord] = []
    throttle = kw.pop("throttle", Throttle(tpm=8000, rpm=30, tpd=200_000))
    try:
        stopped = await run_items(
            h, items, RunOptions(tmp_path, **kw), throttle,
            on_record=out.append, done=set(), say=lambda _s: None,
        )  # fmt: skip
    finally:
        root.removeHandler(h.capture)
        root.setLevel(old)
    return stopped, out


async def test_rule_items_pass_with_no_model(
    indexed: sessionmaker[Session], tmp_path: Path
) -> None:
    items = golden("G-ADV-001", "G-PERF-003", "G-PII-001", "G-PII-005", "G-OOS-003", "G-NF-002")
    h = harness(indexed, tmp_path, None)
    stopped, out = await run(h, items, tmp_path, no_llm=True)
    assert stopped == "complete"
    assert {r.id: r.passed for r in out} == dict.fromkeys([i.id for i in items], True), [
        (r.id, r.grades) for r in out if not r.passed
    ]
    assert h.llm.calls == 0
    assert h.store.count_messages() == 0  # every eval thread is deleted afterwards


async def test_model_item_graded_from_a_scripted_answer(
    indexed: sessionmaker[Session], tmp_path: Path
) -> None:
    llm = FakeLLM(
        router_says("hdfc-mid-cap", query="exit load of HDFC Mid Cap Fund"),
        answer_says(
            "HDFC Mid Cap Fund Direct Growth has an exit load of 1% if redeemed within 1 year.",
            cite="fact:hdfc-mid-cap:exit_load",
        ),
    )
    h = harness(indexed, tmp_path, llm)
    stopped, out = await run(h, golden("G-EXL-003"), tmp_path)
    assert stopped == "complete"
    (r,) = out
    assert r.passed, r.grades
    assert r.llm_calls == 2 and r.tokens["total_tokens"] == 200
    assert r.recall_hit is not None  # the fake embedder may or may not rank it


async def test_busy_reply_stops_the_run(indexed: sessionmaker[Session], tmp_path: Path) -> None:
    llm = FakeLLM(JsonResult(False, error=RATE_LIMITED, retry_after_s=3600))
    h = harness(indexed, tmp_path, llm)
    stopped, out = await run(h, golden("G-EXL-003"), tmp_path)
    assert stopped == "rate_limited"
    assert out == []  # nothing recorded: the item runs again on --resume


async def test_daily_reserve_stops_before_model_items(
    indexed: sessionmaker[Session], tmp_path: Path
) -> None:
    h = harness(indexed, tmp_path, FakeLLM())
    throttle = Throttle(tpm=8000, rpm=30, tpd=21_000, reserve=20_000)
    stopped, out = await run(h, golden("G-PII-001", "G-EXL-003"), tmp_path, throttle=throttle)
    assert stopped == "budget"
    assert [r.id for r in out] == ["G-PII-001"]  # the free item still ran


async def test_throttle_waits_for_the_minute_window() -> None:
    clock = Clock()
    waits: list[float] = []

    async def sleep(s: float) -> None:
        waits.append(s)
        clock.t += s

    t = Throttle(tpm=8000, rpm=30, tpd=200_000, share=0.8, clock=clock, sleep=sleep)
    t.record(5000, 2)
    assert await t.wait_for_room(1000, 3) == 0  # 6000 <= 6400
    t.record(1000, 3)
    waited = await t.wait_for_room(2500, 3)
    assert waited == pytest.approx(60.0)
    assert t.day_tokens == 6000


def test_order_puts_free_items_first() -> None:
    items = golden("G-EXL-003", "G-PII-001", "G-TER-001", "G-ADV-001")
    assert [i.id for i in order_items(items)] == [
        "G-PII-001",
        "G-ADV-001",
        "G-EXL-003",
        "G-TER-001",
    ]


def test_progress_round_trip_and_partial_report(tmp_path: Path) -> None:
    items = golden("G-PII-001", "G-ADV-001")
    r = rec("G-PII-001", "pii", type="pii_rejected")
    (tmp_path / "progress.jsonl").write_text(r.to_json() + "\n", encoding="utf-8")
    (tmp_path / "meta.json").write_text(json.dumps({"repeat": 1, "model": "m"}), encoding="utf-8")
    assert read_progress(tmp_path) == [r]
    _summary, partial = write_report(tmp_path, items)
    assert partial
    md = (tmp_path / "report.md").read_text(encoding="utf-8")
    assert "PARTIAL REPORT" in md and "NOT PASSED" in md
    assert json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))["summary"]["partial"]
