"""Unguessable, time-sortable IDs for threads and messages (architecture §9.1, EC-THR-11).

IDs are ULIDs (48-bit millisecond time + 80 random bits, Crockford base32) with a prefix:
`thr_01H...` for threads, `msg_01H...` for messages. The generator is monotonic within one
process, so IDs made in the same millisecond still sort in creation order; the store relies
on that to keep a thread's messages in send order.
"""

from __future__ import annotations

import secrets
import threading
import time
from typing import Final

_CROCKFORD: Final = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_RANDOM_BITS: Final = 80
_RANDOM_MAX: Final = (1 << _RANDOM_BITS) - 1

ULID_PATTERN: Final = "[0-9A-HJKMNP-TV-Z]{26}"
THREAD_PREFIX: Final = "thr_"
MESSAGE_PREFIX: Final = "msg_"
THREAD_ID_PATTERN: Final = f"^{THREAD_PREFIX}{ULID_PATTERN}$"
MESSAGE_ID_PATTERN: Final = f"^{MESSAGE_PREFIX}{ULID_PATTERN}$"


def _encode(value: int) -> str:
    chars = []
    for _ in range(26):
        chars.append(_CROCKFORD[value & 0x1F])
        value >>= 5
    return "".join(reversed(chars))


class _Monotonic:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last_ms = -1
        self._last_rand = 0

    def next(self, now_ms: int | None = None) -> str:
        ms = int(time.time() * 1000) if now_ms is None else now_ms
        with self._lock:
            if ms <= self._last_ms:
                # Same millisecond, or the clock stepped back: keep sorting forward.
                ms = self._last_ms
                rand = self._last_rand + 1
                if rand > _RANDOM_MAX:  # pragma: no cover - 2^80 IDs in one ms
                    ms, rand = ms + 1, secrets.randbits(_RANDOM_BITS)
            else:
                rand = secrets.randbits(_RANDOM_BITS)
            self._last_ms, self._last_rand = ms, rand
        return _encode((ms << _RANDOM_BITS) | rand)


_gen = _Monotonic()


def new_ulid() -> str:
    return _gen.next()


def new_thread_id() -> str:
    return THREAD_PREFIX + new_ulid()


def new_message_id() -> str:
    return MESSAGE_PREFIX + new_ulid()
