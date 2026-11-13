"""Groq usage counters and the global budget guard (tasks 3.4 and 6.4).

Counts requests and tokens in the current minute and day (UTC), and warns when usage passes
the headroom share of a limit. `check()` is the budget guard: before each call the client
asks whether the call would take this process over `limit x headroom` for any of Groq's
four limits, and if so returns the busy reply without calling Groq.

Groq's limits are per organisation, so this process's own counters aren't the whole
picture (the eval or an ingest may share the key). The guard therefore also uses the
`x-ratelimit-remaining-*` headers from the last response: if Groq says fewer tokens (per
minute) or requests (per day) are left than this call needs plus the share kept for others,
the call waits for the reset instead.

Groq doesn't count cached tokens toward its limits, so they're subtracted here too.
"""

from __future__ import annotations

import logging
import math
import re
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Limits:
    rpm: int
    rpd: int
    tpm: int
    tpd: int
    headroom: float = 0.8


@dataclass
class Window:
    key: int
    requests: int = 0
    tokens: int = 0


@dataclass(frozen=True)
class BudgetCheck:
    """Whether a call may go ahead; when not, which limit and how long until it resets."""

    ok: bool
    limit: str | None = None
    retry_after_s: float | None = None


_DURATION_PART = re.compile(r"(\d+(?:\.\d+)?)(ms|h|m|s)")
_UNIT_S = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0}


def parse_reset(value: str | None) -> float | None:
    """Groq's reset headers, e.g. "7.66s", "2m59.56s", "1h2m", "120ms", to seconds."""
    if not value:
        return None
    text = value.strip()
    try:
        return max(float(text), 0.0)  # plain seconds
    except ValueError:
        pass
    parts = _DURATION_PART.findall(text)
    if not parts or "".join(n + u for n, u in parts) != text:
        return None
    return sum(float(n) * _UNIT_S[u] for n, u in parts)


@dataclass
class _Remaining:
    """What Groq last said is left in a window, and when that window resets."""

    value: int
    until: float  # clock time when the window resets


class UsageTracker:
    def __init__(self, limits: Limits, clock: Callable[[], float] = time.time) -> None:
        self.limits = limits
        self._clock = clock
        self._lock = threading.Lock()
        self._minute = Window(-1)
        self._day = Window(-1)
        self._warned: set[tuple[str, int]] = set()
        self._remaining_tokens: _Remaining | None = None  # per minute
        self._remaining_requests: _Remaining | None = None  # per day

    def _windows(self) -> tuple[Window, Window]:
        now = self._clock()
        minute, day = int(now // 60), int(now // 86400)
        if self._minute.key != minute:
            self._minute = Window(minute)
        if self._day.key != day:
            self._day = Window(day)
        return self._minute, self._day

    def record(self, *, requests: int = 1, tokens: int = 0) -> None:
        with self._lock:
            minute, day = self._windows()
            minute.requests += requests
            day.requests += requests
            minute.tokens += tokens
            day.tokens += tokens
            self._warn_if_close(minute, day)

    def observe_headers(self, headers: Mapping[str, str] | None) -> None:
        """Remember Groq's own count of what's left, from a response's rate-limit headers."""
        if not headers:
            return
        now = self._clock()
        with self._lock:
            for name, attr in (
                ("tokens", "_remaining_tokens"),
                ("requests", "_remaining_requests"),
            ):
                raw = headers.get(f"x-ratelimit-remaining-{name}")
                reset = parse_reset(headers.get(f"x-ratelimit-reset-{name}"))
                try:
                    value = int(float(raw)) if raw is not None else None
                except ValueError:
                    value = None
                if value is not None and reset is not None:
                    setattr(self, attr, _Remaining(value, now + reset))

    def check(self, est_tokens: int) -> BudgetCheck:
        """The budget guard: may a call estimated at `est_tokens` go ahead now?"""
        lim = self.limits
        now = self._clock()
        with self._lock:
            minute, day = self._windows()
            to_minute = 60 - now % 60
            to_day = 86400 - now % 86400
            checks = (
                ("requests/min", minute.requests + 1, lim.rpm, to_minute),
                ("tokens/min", minute.tokens + est_tokens, lim.tpm, to_minute),
                ("requests/day", day.requests + 1, lim.rpd, to_day),
                ("tokens/day", day.tokens + est_tokens, lim.tpd, to_day),
            )
            for name, would_use, limit, wait in checks:
                if would_use > limit * lim.headroom:
                    return BudgetCheck(False, name, wait)
            # Organisation-wide view from Groq's headers: keep (1 - headroom) for others.
            for name, rem, need, limit in (
                ("groq tokens/min", self._remaining_tokens, est_tokens, lim.tpm),
                ("groq requests/day", self._remaining_requests, 1, lim.rpd),
            ):
                if rem is not None and now < rem.until:
                    reserve = math.floor(limit * (1 - lim.headroom))
                    if rem.value - need < reserve:
                        return BudgetCheck(False, name, rem.until - now)
        return BudgetCheck(True)

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            minute, day = self._windows()
            return {
                "minute_requests": minute.requests,
                "minute_tokens": minute.tokens,
                "day_requests": day.requests,
                "day_tokens": day.tokens,
            }

    def _warn_if_close(self, minute: Window, day: Window) -> None:
        lim = self.limits
        checks = (
            ("requests/min", minute.requests, lim.rpm, minute.key),
            ("tokens/min", minute.tokens, lim.tpm, minute.key),
            ("requests/day", day.requests, lim.rpd, day.key),
            ("tokens/day", day.tokens, lim.tpd, day.key),
        )
        for name, used, limit, window in checks:
            if used >= limit * lim.headroom and (name, window) not in self._warned:
                self._warned.add((name, window))
                log.warning(
                    "groq usage at %d%% of %s limit (%d of %d)",
                    round(100 * used / limit),
                    name,
                    used,
                    limit,
                )
