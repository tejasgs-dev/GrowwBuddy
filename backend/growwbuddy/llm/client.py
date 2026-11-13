"""One wrapper around `AsyncGroq` applying the settings in "LLM Provider: Groq" (task 3.4).

Every failure comes back as `JsonResult(ok=False)` instead of an exception, so callers always
take a safe path. Only `finish_reason == "stop"` with non-empty, parseable JSON and no refusal
is accepted. Groq has no server-side refusal fallback.

Retries: this wrapper is the **only** retry layer (the SDK's own retries are turned off).
The SDK would wait up to 60 s for any `retry-after`, and would keep retrying a long one with
short backoff. Here, a 429 is retried only when its `retry-after` is at most
`llm_max_retry_wait_s`; a longer wait (for example the daily limit) returns
`error="rate_limited"` at once, which the pipeline turns into the busy reply.

Budget guard (task 6.4): before every attempt, the usage tracker checks the call against
`GROWWBUDDY_GROQ_*` x `GROWWBUDDY_GROQ_HEADROOM` and Groq's own remaining-quota headers.
A call that would go over is not sent; it returns `error="rate_limited"`, so the user gets
the busy reply and the shared organisation budget is never overrun.

Every call logs one line with token usage and Groq's rate-limit headers, and never the
message or the model's reasoning text (§11).
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import random
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from growwbuddy.llm.usage import Limits, UsageTracker
from growwbuddy.obs import record_llm_call

if TYPE_CHECKING:
    from growwbuddy.config import Settings

log = logging.getLogger(__name__)

RATE_LIMITED = "rate_limited"


@dataclass(frozen=True)
class JsonResult:
    ok: bool
    data: dict[str, Any] | None = None
    error: str | None = None
    finish_reason: str | None = None
    usage: dict[str, int] = field(default_factory=dict)
    retry_after_s: float | None = None

    @property
    def rate_limited(self) -> bool:
        return self.error == RATE_LIMITED


class StructuredLLM(Protocol):
    async def complete_json(
        self,
        *,
        system: str,
        user: str,
        schema_name: str,
        schema: dict[str, Any],
        max_tokens: int,
        kind: str = ...,
    ) -> JsonResult: ...


def strict_format(schema_name: str, schema: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "json_schema",
        "json_schema": {"name": schema_name, "strict": True, "schema": schema},
    }


def read_usage(completion: Any) -> dict[str, int]:
    u = getattr(completion, "usage", None)
    if u is None:
        return {}
    usage = {
        "prompt_tokens": int(getattr(u, "prompt_tokens", 0) or 0),
        "completion_tokens": int(getattr(u, "completion_tokens", 0) or 0),
    }
    details = getattr(u, "prompt_tokens_details", None)
    usage["cached_tokens"] = int(getattr(details, "cached_tokens", 0) or 0)
    usage["total_tokens"] = usage["prompt_tokens"] + usage["completion_tokens"]
    return usage


def read_completion(completion: Any) -> JsonResult:
    """Turn a chat completion into a JsonResult, rejecting anything but a clean stop."""
    usage = read_usage(completion)
    choices = getattr(completion, "choices", None) or []
    if not choices:
        return JsonResult(False, error="no choices in response", usage=usage)
    choice = choices[0]
    reason = getattr(choice, "finish_reason", None)
    if reason != "stop":
        return JsonResult(False, error=f"finish_reason={reason}", finish_reason=reason, usage=usage)
    message = getattr(choice, "message", None)
    if getattr(message, "refusal", None):
        return JsonResult(False, error="model refused", finish_reason=reason, usage=usage)
    content = getattr(message, "content", None)
    if not content or not str(content).strip():
        return JsonResult(False, error="empty message", finish_reason=reason, usage=usage)
    try:
        data = json.loads(content)
    except json.JSONDecodeError as e:
        return JsonResult(False, error=f"invalid JSON: {e}", finish_reason=reason, usage=usage)
    if not isinstance(data, dict):
        return JsonResult(False, error="JSON is not an object", finish_reason=reason, usage=usage)
    return JsonResult(True, data=data, finish_reason=reason, usage=usage)


def parse_retry_after(headers: Mapping[str, str] | None) -> float | None:
    if not headers:
        return None
    raw = headers.get("retry-after")
    if raw is None:
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    return value if value >= 0 else None


_RATE_HEADERS = (
    "x-ratelimit-remaining-tokens",
    "x-ratelimit-remaining-requests",
    "x-ratelimit-reset-tokens",
    "x-ratelimit-reset-requests",
)


def rate_headers(headers: Mapping[str, str] | None) -> dict[str, str]:
    if not headers:
        return {}
    return {h.removeprefix("x-ratelimit-"): headers[h] for h in _RATE_HEADERS if h in headers}


@dataclass(frozen=True)
class _Attempt:
    """What one HTTP attempt produced, independent of the SDK's types."""

    completion: Any = None
    headers: Mapping[str, str] | None = None
    status: int | None = None  # set on an HTTP error
    error: str | None = None  # exception class name on failure


# Retryable: rate limits, server errors, timeouts and connection problems.
def _retryable(a: _Attempt) -> bool:
    if a.status is not None:
        return a.status == 429 or a.status >= 500
    return a.error in ("APITimeoutError", "APIConnectionError")


Sender = Callable[[dict[str, Any]], Awaitable[_Attempt]]

# Rough token count for the budget guard: ~4 characters per token for the prompt, plus a
# quarter of the completion cap (answers are short; the cap leaves room for reasoning).
_CHARS_PER_TOKEN = 4


def estimate_tokens(system: str, user: str, max_tokens: int) -> int:
    return math.ceil((len(system) + len(user)) / _CHARS_PER_TOKEN) + max_tokens // 4


class GroqClient:
    """Lazily creates the AsyncGroq client, so GROQ_API_KEY is needed only on a real call."""

    def __init__(
        self,
        s: Settings | None = None,
        *,
        usage: UsageTracker | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        send: Sender | None = None,
    ) -> None:
        if s is None:
            from growwbuddy.config import settings

            s = settings
        self._settings: Settings = s
        self._client: Any = None
        self._sleep = sleep
        self._send: Sender = send or self._send_groq
        self.usage = usage or UsageTracker(
            Limits(s.groq_rpm, s.groq_rpd, s.groq_tpm, s.groq_tpd, s.groq_headroom)
        )

    def _get(self) -> Any:
        if self._client is None:
            from groq import AsyncGroq

            self._client = AsyncGroq(
                api_key=self._settings.require_groq_api_key(),
                max_retries=0,  # retries are handled here, in one place
                timeout=self._settings.llm_timeout_s,
            )
        return self._client

    async def _send_groq(self, request: dict[str, Any]) -> _Attempt:
        import groq

        try:
            raw = await self._get().chat.completions.with_raw_response.create(**request)
            return _Attempt(completion=await raw.parse(), headers=raw.headers)
        except groq.APIStatusError as e:
            return _Attempt(
                headers=e.response.headers, status=e.status_code, error=type(e).__name__
            )
        except Exception as e:  # timeout, connection, missing key: all -> safe fallback
            return _Attempt(error=type(e).__name__)

    def _backoff(self, attempt: int) -> float:
        return float(min(2**attempt, 8)) * (0.75 + random.random() / 2)  # ~1 s, 2 s, 4 s

    async def complete_json(
        self,
        *,
        system: str,
        user: str,
        schema_name: str,
        schema: dict[str, Any],
        max_tokens: int,
        kind: str = "call",
    ) -> JsonResult:
        request: dict[str, Any] = {
            "model": self._settings.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "response_format": strict_format(schema_name, schema),
            "temperature": 0,
            "reasoning_effort": self._settings.reasoning_effort,
            "include_reasoning": False,
            "max_completion_tokens": max_tokens,
        }
        max_retries = self._settings.llm_max_retries
        max_wait = self._settings.llm_max_retry_wait_s
        started = time.monotonic()
        est = estimate_tokens(system, user, max_tokens)

        for attempt in range(max_retries + 1):
            budget = self.usage.check(est)
            if not budget.ok:
                log.warning(
                    "groq budget guard: call not sent kind=%s limit=%s est_tokens=%d "
                    "retry_after=%s window=%s",
                    kind,
                    budget.limit,
                    est,
                    None if budget.retry_after_s is None else round(budget.retry_after_s, 1),
                    self.usage.snapshot(),
                )
                return JsonResult(False, error=RATE_LIMITED, retry_after_s=budget.retry_after_s)
            a = await self._send(request)
            self.usage.record(requests=1)
            self.usage.observe_headers(a.headers)
            if a.completion is not None:
                result = read_completion(a.completion)
                billable = result.usage.get("total_tokens", 0) - result.usage.get(
                    "cached_tokens", 0
                )
                self.usage.record(requests=0, tokens=max(billable, 0))
                self._log(kind, result, a, started, attempt)
                return result

            retry_after = parse_retry_after(a.headers)
            if a.status == 429:
                if retry_after is not None and retry_after > max_wait:
                    # e.g. the daily limit: hours away, don't keep the user waiting.
                    return self._fail(kind, a, started, attempt, RATE_LIMITED, retry_after)
            elif not _retryable(a):
                return self._fail(kind, a, started, attempt, f"api error: {a.error}", None)

            if attempt == max_retries:
                error = RATE_LIMITED if a.status == 429 else f"api error: {a.error}"
                return self._fail(kind, a, started, attempt, error, retry_after)
            wait = retry_after if retry_after is not None else self._backoff(attempt)
            await self._sleep(min(wait, max_wait))

        raise AssertionError("unreachable")  # pragma: no cover

    def _fail(
        self,
        kind: str,
        a: _Attempt,
        started: float,
        attempt: int,
        error: str,
        retry_after: float | None,
    ) -> JsonResult:
        result = JsonResult(False, error=error, retry_after_s=retry_after)
        self._log(kind, result, a, started, attempt)
        return result

    def _log(
        self, kind: str, result: JsonResult, a: _Attempt, started: float, attempt: int
    ) -> None:
        # Never log message text or reasoning: only counts, status and headers.
        record_llm_call(result.usage)  # per-request token totals for the summary line (4.8)
        log.info(
            "groq call kind=%s model=%s ok=%s finish=%s error=%s status=%s retries=%d "
            "latency_ms=%d usage=%s ratelimit=%s retry_after=%s window=%s",
            kind,
            self._settings.model,
            result.ok,
            result.finish_reason,
            result.error,
            a.status,
            attempt,
            round(1000 * (time.monotonic() - started)),
            result.usage,
            rate_headers(a.headers),
            result.retry_after_s,
            self.usage.snapshot(),
        )
