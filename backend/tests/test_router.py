"""Stage 2 LLM router (task 3.7; EV-3-06)."""

from __future__ import annotations

from growwbuddy.llm.client import RATE_LIMITED, JsonResult
from growwbuddy.llm.prompts import load_prompt
from growwbuddy.pipeline.router import classify, render_history
from growwbuddy.pipeline.types import Turn
from tests.fakes import FakeLLM, ok, router_says


async def test_classify_returns_decision_and_passes_history() -> None:
    llm = FakeLLM(router_says("hdfc-mid-cap", query="What is the exit load of HDFC Mid Cap?"))
    history = [Turn("TER of mid cap?", "answer", "hdfc-mid-cap")]
    out = await classify("and its exit load?", history, llm, history_turns=6)
    assert out.decision is not None
    assert out.decision.scheme == "hdfc-mid-cap"
    call = llm.calls[0]
    assert call["kind"] == "router"
    assert call["system"] == load_prompt("router_system")  # history never in the system prompt
    assert "TER of mid cap?" in call["user"] and "and its exit load?" in call["user"]


async def test_EV_3_06_unsure_goes_to_advisory() -> None:
    llm = FakeLLM(router_says("hdfc-mid-cap", intent="factual", may_be_advice=True))
    out = await classify("exit load, and is it good?", [], llm, 6)
    assert out.decision is not None and out.decision.final_intent == "advisory"


async def test_invalid_output_is_a_failure_not_an_exception() -> None:
    llm = FakeLLM(ok({"intent": "nonsense"}))
    out = await classify("hello?", [], llm, 6)
    assert out.decision is None and not out.result.ok


async def test_rate_limit_is_passed_through() -> None:
    llm = FakeLLM(JsonResult(False, error=RATE_LIMITED, retry_after_s=60))
    out = await classify("x", [], llm, 6)
    assert out.decision is None and out.result.rate_limited


def test_history_is_trimmed_to_recent_turns() -> None:
    turns = [Turn(f"q{i}", "answer", "hdfc-mid-cap") for i in range(10)]
    text = render_history(turns, 3)
    assert "q9" in text and "q7" in text and "q6" not in text
    assert render_history([], 6) == "(no earlier turns)"
