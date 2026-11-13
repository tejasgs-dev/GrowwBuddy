"""Per-thread locks so messages in one thread are handled in send order (task 4.3, §9.2).

Different threads never wait on each other. `asyncio.Lock` wakes waiters first-in,
first-out, so two quick messages in one thread are processed in the order they arrived
(EC-THR-06). Locks for idle threads are dropped, so the registry doesn't grow without
bound (EC-THR-16).

These locks only work inside one process: run the API with a single worker (EC-THR-08).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field


@dataclass
class _Entry:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    users: int = 0  # holders plus waiters
    last_used: float = 0.0


class ThreadLocks:
    def __init__(
        self,
        *,
        idle_s: float = 600.0,
        sweep_every_s: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.idle_s = idle_s
        self.sweep_every_s = sweep_every_s
        self._clock = clock
        self._entries: dict[str, _Entry] = {}
        self._last_sweep = clock()

    def __len__(self) -> int:
        return len(self._entries)

    @asynccontextmanager
    async def hold(self, thread_id: str) -> AsyncIterator[None]:
        entry = self._entries.get(thread_id)
        if entry is None:
            entry = self._entries[thread_id] = _Entry()
        entry.users += 1
        try:
            async with entry.lock:
                yield
        finally:
            entry.users -= 1
            entry.last_used = self._clock()
            self._maybe_sweep()

    def _maybe_sweep(self) -> None:
        if self._clock() - self._last_sweep >= self.sweep_every_s:
            self.sweep()

    def sweep(self) -> int:
        """Drop locks nobody holds or waits on and that have been idle for `idle_s`."""
        now = self._clock()
        self._last_sweep = now
        idle = [
            tid
            for tid, e in self._entries.items()
            if e.users == 0 and not e.lock.locked() and now - e.last_used >= self.idle_s
        ]
        for tid in idle:
            del self._entries[tid]
        return len(idle)
