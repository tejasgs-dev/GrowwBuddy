"""Per-client and per-IP limits on model-answered messages (task 6.4, layer 2).

Groq's free tier allows only about 3 model-answered messages a minute and 80 a day across
all users, so one user mustn't be able to use up the shared budget. Each `client_id`, and
each IP address (so a new `client_id` per request doesn't get around it, EC-API-05), may
send a limited number of messages a minute and a day **that need the model**.

Messages the rules handle (PII, advisory, NAV and performance, greeting, out of scope) cost
no tokens, so they are never counted or blocked. That is why this is a small limiter of
our own rather than `slowapi`: slowapi's decorators count every request before the route
runs, and the rule check has to happen first.

In-memory sliding windows, like the per-thread locks: run one worker (EC-THR-08).
"""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

MINUTE = 60.0
DAY = 86400.0
_PRUNE_EVERY = 1000  # calls between sweeps of idle keys


@dataclass(frozen=True)
class LimitCheck:
    ok: bool
    scope: str | None = None  # e.g. "client/min", "ip/day"
    retry_after_s: float | None = None


@dataclass(frozen=True)
class LimitConfig:
    client_per_minute: int = 2
    client_per_day: int = 20
    ip_per_minute: int = 4
    ip_per_day: int = 40


class MessageLimiter:
    def __init__(self, config: LimitConfig, clock: Callable[[], float] = time.time) -> None:
        self.config = config
        self._clock = clock
        self._lock = threading.Lock()
        self._hits: dict[str, deque[float]] = {}  # key -> send times in the last day
        self._calls = 0

    def try_acquire(self, client_id: str, ip: str | None) -> LimitCheck:
        """Count one model-answered message, or say which limit it would break."""
        c = self.config
        now = self._clock()
        keys = [(f"client:{client_id}", "client", c.client_per_minute, c.client_per_day)]
        if ip:
            keys.append((f"ip:{ip}", "ip", c.ip_per_minute, c.ip_per_day))
        with self._lock:
            self._maybe_prune(now)
            for key, scope, per_minute, per_day in keys:
                stamps = self._window(key, now)
                if len(stamps) >= per_day:
                    return LimitCheck(False, f"{scope}/day", stamps[0] + DAY - now)
                recent = [t for t in stamps if t > now - MINUTE]
                if len(recent) >= per_minute:
                    return LimitCheck(False, f"{scope}/min", recent[0] + MINUTE - now)
            for key, *_ in keys:
                self._hits.setdefault(key, deque()).append(now)
        return LimitCheck(True)

    def _window(self, key: str, now: float) -> deque[float]:
        stamps = self._hits.get(key)
        if stamps is None:
            return deque()
        while stamps and stamps[0] <= now - DAY:
            stamps.popleft()
        return stamps

    def _maybe_prune(self, now: float) -> None:
        self._calls += 1
        if self._calls % _PRUNE_EVERY:
            return
        for key in [k for k, v in self._hits.items() if not v or v[-1] <= now - DAY]:
            del self._hits[key]

    def __len__(self) -> int:
        return len(self._hits)
