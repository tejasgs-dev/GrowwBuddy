"""Answer generator (task 3.9; EV-3-17)."""

from __future__ import annotations

from datetime import date

import pytest

from growwbuddy.llm.client import RATE_LIMITED, JsonResult
from growwbuddy.llm.prompts import load_prompt
from growwbuddy.pipeline.generator import build_user_message, classify_failure, generate
from growwbuddy.retrieval.retriever import ContextItem
from tests.fakes import FakeLLM, answer_says, ok

ITEMS = [
    ContextItem(
        "fact:hdfc-mid-cap:exit_load", "fact", "hdfc-mid-cap",
        "Exit load of HDFC Mid Cap Fund Direct Growth: Exit load of 1% if redeemed within 1 year.",
        "https://groww.in/x", date(2026, 11, 11), date(2026, 11, 11), None,
    ),
    ContextItem(
        "groww-hdfc-mid-cap#exit_load", "chunk", "hdfc-mid-cap", "Exit load text.",
        "https://groww.in/x", date(2026, 11, 11), None, None, 0.9,
    ),
]  # fmt: skip


def test_context_is_labelled_by_id() -> None:
    msg = build_user_message("What is the exit load?", ITEMS)
    assert "[fact:hdfc-mid-cap:exit_load] Verified fact. Exit load of" in msg
    assert "[groww-hdfc-mid-cap#exit_load] Exit load text." in msg
    assert msg.endswith("Question: What is the exit load?")


async def test_generate_parses_answer() -> None:
    llm = FakeLLM(answer_says("It is 1%.", cite="groww-hdfc-mid-cap#exit_load"))
    g = await generate("q", ITEMS, llm)
    assert g.failure is None and g.answer is not None
    assert g.answer.source_chunk_id == "groww-hdfc-mid-cap#exit_load"
    assert llm.calls[0]["kind"] == "answer"
    assert llm.calls[0]["system"] == load_prompt("answer_system")


async def test_retry_sends_feedback() -> None:
    llm = FakeLLM(answer_says("It is 1%.", cite="x"))
    await generate("q", ITEMS, llm, feedback=("the answer has 4 sentences",))
    assert llm.calls[0]["kind"] == "answer_retry"
    assert "rejected: the answer has 4 sentences" in llm.calls[0]["user"]


async def test_schema_violation_is_retryable() -> None:
    llm = FakeLLM(answer_says("a.", "b.", "c.", "d.", cite="x"))
    g = await generate("q", ITEMS, llm)
    assert g.failure == "schema" and g.answer is None


@pytest.mark.parametrize(
    ("result", "failure"),
    [
        (JsonResult(False, error=RATE_LIMITED), "busy"),
        (JsonResult(False, error="api error: APITimeoutError"), "unavailable"),
        (JsonResult(False, error="finish_reason=length", finish_reason="length"), "bad_output"),
        (JsonResult(False, error="empty message"), "bad_output"),
        (JsonResult(False, error="invalid JSON: x"), "bad_output"),
        (JsonResult(False, error="model refused"), "bad_output"),
    ],
)
async def test_EV_3_17_failures_are_sorted(result: JsonResult, failure: str) -> None:
    assert classify_failure(result) == failure
    g = await generate("q", ITEMS, FakeLLM(result))
    assert g.failure == failure and g.answer is None


async def test_answer_with_wrong_shape_is_schema_failure() -> None:
    g = await generate("q", ITEMS, FakeLLM(ok({"answerable": True})))
    assert g.failure == "schema"
