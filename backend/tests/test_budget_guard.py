"""Global Groq budget guard (task 6.4, layer 1). HTTP is replaced by an injected `send`."""

from __future__ import annotations

from typing import Any

import pytest

from growwbuddy.config import Settings
from growwbuddy.llm.client import RATE_LIMITED, GroqClient, _Attempt, estimate_tokens
from growwbuddy.llm.usage import Limits, UsageTracker, parse_reset
from tests.test_llm_retries import Script, Sleeps, ok_attempt


class Clock:
    def __init__(self, t: float = 1_000_040.0) -> None:  # 20 s into a minute
        self.t = t

    def __call__(self) -> float:
        return self.t


def guarded(
    script: Script, clock: Clock, *, rpm: int = 30, tpm: int = 8000, rpd: int = 1000,
    tpd: int = 200_000, headroom: float = 0.8,
) -> GroqClient:  # fmt: skip
    s = Settings(_env_file=None)
    usage = UsageTracker(Limits(rpm, rpd, tpm, tpd, headroom), clock=clock)
    return GroqClient(s, send=script, sleep=Sleeps(), usage=usage)


async def call(client: GroqClient, system: str = "s" * 400, user: str = "u") -> Any:
    return await client.complete_json(
        system=system, user=user, schema_name="x", schema={}, max_tokens=512, kind="answer"
    )


def test_estimate_counts_prompt_and_a_share_of_the_cap() -> None:
    assert estimate_tokens("a" * 400, "b" * 400, 1024) == 200 + 256


async def test_minute_token_budget_blocks_without_calling_groq_then_resets() -> None:
    clock = Clock()
    # Each call bills 250 tokens (completion() in test_llm_retries); est ~ 229.
    script = Script(*[ok_attempt() for _ in range(10)])
    client = guarded(script, clock, tpm=1000, headroom=0.8)  # 800 tokens a minute
    assert (await call(client)).ok
    assert (await call(client)).ok  # 500 used
    assert (await call(client)).ok  # 500 + ~229 <= 800
    sent = len(script.requests)
    refused = await call(client)  # 750 + 229 > 800
    assert not refused.ok and refused.error == RATE_LIMITED
    assert len(script.requests) == sent  # Groq was never called
    assert refused.retry_after_s == pytest.approx(40.0)  # until the minute rolls over
    clock.t += 41
    assert (await call(client)).ok


async def test_minute_request_budget() -> None:
    clock = Clock()
    script = Script(*[ok_attempt() for _ in range(5)])
    client = guarded(script, clock, rpm=5, headroom=0.8)  # 4 requests a minute
    for _ in range(4):
        assert (await call(client)).ok
    r = await call(client)
    assert r.error == RATE_LIMITED
    assert len(script.requests) == 4


async def test_daily_token_budget_blocks_until_the_next_day() -> None:
    clock = Clock()
    script = Script(*[ok_attempt() for _ in range(5)])
    client = guarded(script, clock, tpd=1000, headroom=0.8)
    for _ in range(3):  # 750 billed
        assert (await call(client)).ok
    r = await call(client)
    assert r.error == RATE_LIMITED
    assert r.retry_after_s is not None and r.retry_after_s > 3600  # hours away
    clock.t += 86400
    assert (await call(client)).ok


async def test_groq_headers_show_other_processes_usage() -> None:
    """The eval or an ingest may share the key: trust Groq's remaining-tokens count."""
    clock = Clock()
    low = _Attempt(
        completion=ok_attempt().completion,
        headers={"x-ratelimit-remaining-tokens": "1500", "x-ratelimit-reset-tokens": "12.5s"},
    )
    script = Script(low, ok_attempt())
    client = guarded(script, clock)  # reserve for others: 20% of 8000 = 1600
    assert (await call(client)).ok
    r = await call(client)  # 1500 - est < 1600
    assert r.error == RATE_LIMITED
    assert r.retry_after_s == pytest.approx(12.5)
    assert len(script.requests) == 1
    clock.t += 13  # Groq's window reset
    assert (await call(client)).ok


def test_parse_reset_formats() -> None:
    assert parse_reset("7.66s") == pytest.approx(7.66)
    assert parse_reset("2m59.56s") == pytest.approx(179.56)
    assert parse_reset("1h2m") == pytest.approx(3720)
    assert parse_reset("120ms") == pytest.approx(0.12)
    assert parse_reset("30") == 30
    assert parse_reset("soon") is None
    assert parse_reset(None) is None


async def test_busy_reply_when_the_guard_refuses(indexed: Any, retrieval_headers: Any) -> None:
    """The orchestrator turns the guard's refusal into the busy reply (task 3.2)."""
    from tests.test_orchestrator import make_pipeline

    clock = Clock()
    client = guarded(Script(), clock, rpm=1, headroom=0.5)  # no request allowed at all
    pipe = make_pipeline(indexed, client, retrieval_headers)
    resp = await pipe.handle_message("Who is the registrar of HDFC Mid Cap Fund?")
    assert resp.type == "busy"
    assert resp.citation is None and resp.footer is None
