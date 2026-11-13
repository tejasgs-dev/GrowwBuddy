"""Thread and message IDs: unguessable, and sortable in creation order (§9.1, EC-THR-11)."""

from __future__ import annotations

import re

from growwbuddy.ids import (
    MESSAGE_ID_PATTERN,
    THREAD_ID_PATTERN,
    _Monotonic,
    new_message_id,
    new_thread_id,
)


def test_formats() -> None:
    assert re.match(THREAD_ID_PATTERN, new_thread_id())
    assert re.match(MESSAGE_ID_PATTERN, new_message_id())
    assert not re.match(THREAD_ID_PATTERN, new_message_id())


def test_ids_sort_in_creation_order() -> None:
    ids = [new_message_id() for _ in range(2000)]  # many share a millisecond
    assert ids == sorted(ids)
    assert len(set(ids)) == len(ids)


def test_same_millisecond_and_clock_going_back_still_sort_forward() -> None:
    g = _Monotonic()
    a = g.next(now_ms=1_000_000)
    b = g.next(now_ms=1_000_000)
    c = g.next(now_ms=999_000)  # clock stepped back
    d = g.next(now_ms=1_000_001)
    assert a < b < c < d


def test_ids_are_random() -> None:
    # 80 random bits: two IDs never share their random part.
    a, b = new_thread_id(), new_thread_id()
    assert a[-16:] != b[-16:]
