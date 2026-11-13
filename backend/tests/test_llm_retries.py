"""Groq client: 429 handling, one retry layer, usage logging (task 3.4).

HTTP is replaced by an injected `send`, so nothing reaches Groq.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any

import pytest

from growwbuddy.config import Settings
from growwbuddy.llm.client import RATE_LIMITED, GroqClient, _Attempt
from growwbuddy.llm.usage import Limits, UsageTracker

SECRET_TEXT = "my secret question about mid cap"
REASONING = "internal reasoning that must never be logged"


def completion(content: str = '{"a": 1}', finish: str = "stop", cached: int = 0) -> Any:
    msg = SimpleNamespace(content=content, refusal=None, reasoning=REASONING)
    usage = SimpleNamespace(
        prompt_tokens=200,
        completion_tokens=50,
        prompt_tokens_details=SimpleNamespace(cached_tokens=cached),
    )
    return SimpleNamespace(
        choices=[SimpleNamespace(finish_reason=finish, message=msg)], usage=usage
    )


OK_HEADERS = {"x-ratelimit-remaining-tokens": "7000", "x-ratelimit-remaining-requests": "999"}


def ok_attempt(**kw: Any) -> _Attempt:
    return _Attempt(completion=completion(**kw), headers=OK_HEADERS)


def rate_limited(retry_after: str | None) -> _Attempt:
    headers = {} if retry_after is None else {"retry-after": retry_after}
    return _Attempt(headers=headers, status=429, error="RateLimitError")


class Script:
    def __init__(self, *attempts: _Attempt) -> None:
        self.attempts = list(attempts)
        self.requests: list[dict[str, Any]] = []

    async def __call__(self, request: dict[str, Any]) -> _Attempt:
        self.requests.append(request)
        return self.attempts.pop(0)


class Sleeps:
    def __init__(self) -> None:
        self.waits: list[float] = []

    async def __call__(self, s: float) -> None:
        self.waits.append(s)


def make(script: Script, sleeps: Sleeps, **settings: Any) -> GroqClient:
    s = Settings(_env_file=None, **settings)
    return GroqClient(s, send=script, sleep=sleeps)


async def call(client: GroqClient, kind: str = "answer") -> Any:
    return await client.complete_json(
        system="sys", user=SECRET_TEXT, schema_name="x", schema={"type": "object"},
        max_tokens=64, kind=kind,
    )  # fmt: skip


async def test_request_uses_plan_settings() -> None:
    script, sleeps = Script(ok_attempt()), Sleeps()
    r = await call(make(script, sleeps))
    assert r.ok
    req = script.requests[0]
    assert req["model"] == "openai/gpt-oss-120b"
    assert req["temperature"] == 0
    assert req["reasoning_effort"] == "low"
    assert req["include_reasoning"] is False
    assert "reasoning_format" not in req
    assert req["response_format"]["json_schema"]["strict"] is True
    assert "stream" not in req and "tools" not in req


async def test_429_with_short_retry_after_is_retried() -> None:
    script, sleeps = Script(rate_limited("1"), ok_attempt()), Sleeps()
    r = await call(make(script, sleeps))
    assert r.ok
    assert sleeps.waits == [1.0]
    assert len(script.requests) == 2


async def test_429_with_long_retry_after_is_not_waited_on() -> None:
    script, sleeps = Script(rate_limited("3600")), Sleeps()
    r = await call(make(script, sleeps))
    assert not r.ok and r.error == RATE_LIMITED and r.rate_limited
    assert r.retry_after_s == 3600
    assert sleeps.waits == []
    assert len(script.requests) == 1


async def test_repeated_429s_stop_at_the_retry_limit() -> None:
    script = Script(*(rate_limited("1") for _ in range(10)))
    sleeps = Sleeps()
    r = await call(make(script, sleeps, llm_max_retries=2))
    assert r.rate_limited
    assert len(script.requests) == 3  # first try + 2 retries, no more
    assert len(sleeps.waits) == 2


async def test_429_without_retry_after_uses_backoff() -> None:
    script, sleeps = Script(rate_limited(None), rate_limited(None), ok_attempt()), Sleeps()
    r = await call(make(script, sleeps))
    assert r.ok
    assert len(sleeps.waits) == 2
    assert 0.5 <= sleeps.waits[0] <= 1.5  # ~1 s
    assert 1.0 <= sleeps.waits[1] <= 3.0  # ~2 s


async def test_server_errors_retry_then_report_api_error() -> None:
    script = Script(*(_Attempt(status=503, error="InternalServerError") for _ in range(3)))
    r = await call(make(script, Sleeps()))
    assert not r.ok and r.error and r.error.startswith("api error")
    assert not r.rate_limited


async def test_client_errors_are_not_retried() -> None:
    script = Script(_Attempt(status=400, error="BadRequestError"))
    r = await call(make(script, Sleeps()))
    assert not r.ok and r.error == "api error: BadRequestError"
    assert len(script.requests) == 1


async def test_timeout_is_retried() -> None:
    script = Script(_Attempt(error="APITimeoutError"), ok_attempt())
    assert (await call(make(script, Sleeps()))).ok


async def test_none_of_these_raise() -> None:
    for attempt in (
        rate_limited("1"),
        rate_limited("99999"),
        _Attempt(status=401, error="AuthenticationError"),
        _Attempt(error="APIConnectionError"),
    ):
        script = Script(*([attempt] * 5))
        r = await call(make(script, Sleeps()))
        assert not r.ok


async def test_usage_log_has_counts_and_headers_but_no_text(
    caplog: pytest.LogCaptureFixture,
) -> None:
    script = Script(ok_attempt(cached=120))
    with caplog.at_level(logging.INFO, logger="growwbuddy.llm.client"):
        r = await call(make(script, Sleeps()), kind="router")
    assert r.usage["cached_tokens"] == 120
    log = caplog.text
    assert "kind=router" in log
    assert "prompt_tokens" in log and "completion_tokens" in log and "cached_tokens" in log
    assert "finish=stop" in log
    assert "remaining-tokens" in log and "remaining-requests" in log
    assert SECRET_TEXT not in log
    assert REASONING not in log


async def test_429_log_has_retry_after(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="growwbuddy.llm.client"):
        await call(make(Script(rate_limited("3600")), Sleeps()))
    assert "retry_after=3600" in caplog.text


async def test_usage_counts_billable_tokens() -> None:
    script = Script(ok_attempt(cached=100))
    client = make(script, Sleeps())
    await call(client)
    snap = client.usage.snapshot()
    assert snap["minute_requests"] == 1
    assert snap["minute_tokens"] == 150  # 250 total - 100 cached


# ---------------------------------------------------------------- usage tracker


def test_usage_windows_reset() -> None:
    now = [0.0]
    t = UsageTracker(Limits(30, 1000, 8000, 200_000), clock=lambda: now[0])
    t.record(requests=1, tokens=500)
    now[0] = 61.0
    t.record(requests=1, tokens=100)
    snap = t.snapshot()
    assert snap["minute_requests"] == 1 and snap["minute_tokens"] == 100
    assert snap["day_requests"] == 2 and snap["day_tokens"] == 600
    now[0] = 86_401.0
    assert t.snapshot()["day_tokens"] == 0


def test_usage_warns_once_past_headroom(caplog: pytest.LogCaptureFixture) -> None:
    t = UsageTracker(Limits(30, 1000, 1000, 200_000, headroom=0.8), clock=lambda: 0.0)
    with caplog.at_level(logging.WARNING, logger="growwbuddy.llm.usage"):
        t.record(requests=0, tokens=700)
        assert "tokens/min" not in caplog.text
        t.record(requests=0, tokens=200)
        t.record(requests=0, tokens=50)
    assert caplog.text.count("tokens/min") == 1
