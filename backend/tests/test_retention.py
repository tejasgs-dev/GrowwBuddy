"""Thread auto-delete (task 6.7; EV-6-07) and SQLite under write load (EV-6-14)."""

from __future__ import annotations

import asyncio
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import text

from growwbuddy.main import retention_loop
from growwbuddy.pipeline.types import Response
from growwbuddy.store.db import init_db, make_engine, make_session_factory
from growwbuddy.store.threads import ThreadStore
from tests.api_helpers import CLIENT_A, make_services

NOW = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)


class Clock:
    def __init__(self, t: datetime) -> None:
        self.t = t

    def __call__(self) -> datetime:
        return self.t


def store_at(tmp_path: Path, clock: Clock) -> ThreadStore:
    engine = make_engine(f"sqlite:///{(tmp_path / 'r.db').as_posix()}")
    init_db(engine)
    return ThreadStore(make_session_factory(engine), clock=clock)


def reply() -> Response:
    return Response(type="greeting", text="Hi!")


def test_EV_6_07_inactive_threads_deleted_recent_kept(tmp_path: Path) -> None:
    clock = Clock(NOW - timedelta(days=31))
    store = store_at(tmp_path, clock)
    old = store.create(CLIENT_A)
    store.append_exchange(old.id, "hello", reply())
    clock.t = NOW - timedelta(days=29)
    recent = store.create(CLIENT_A)
    store.append_exchange(recent.id, "hello", reply())
    clock.t = NOW

    assert store.delete_inactive(30) == 1
    assert store.get(old.id, CLIENT_A) is None
    assert store.count_messages(old.id) == 0
    assert store.get(recent.id, CLIENT_A) is not None
    assert store.count_messages(recent.id) == 2
    assert store.delete_inactive(30) == 0


def test_activity_keeps_an_old_thread(tmp_path: Path) -> None:
    clock = Clock(NOW - timedelta(days=40))
    store = store_at(tmp_path, clock)
    t = store.create(CLIENT_A)
    clock.t = NOW - timedelta(days=2)
    store.append_exchange(t.id, "hello", reply())  # bumps updated_at
    clock.t = NOW
    assert store.delete_inactive(30) == 0


async def test_retention_loop_sweeps_and_survives_errors(tmp_path: Path) -> None:
    clock = Clock(NOW - timedelta(days=31))
    store = store_at(tmp_path, clock)
    t = store.create(CLIENT_A)
    clock.t = NOW
    svc = make_services(store._sf)  # same database
    svc.store = store
    task = asyncio.create_task(retention_loop(svc, 30, every_s=3600))
    for _ in range(50):
        await asyncio.sleep(0.01)
        if store.get(t.id, CLIENT_A) is None:
            break
    task.cancel()
    assert store.get(t.id, CLIENT_A) is None


def test_EV_6_14_parallel_writers_no_locked_errors(tmp_path: Path) -> None:
    engine = make_engine(f"sqlite:///{(tmp_path / 'load.db').as_posix()}")
    init_db(engine)
    store = ThreadStore(make_session_factory(engine))
    with engine.connect() as conn:
        assert conn.execute(text("PRAGMA journal_mode")).scalar() == "wal"
    threads = [store.create(CLIENT_A) for _ in range(20)]
    errors: list[BaseException] = []

    def write(thread_id: str) -> None:
        try:
            for n in range(10):
                store.append_exchange(thread_id, f"message {n}", reply())
        except BaseException as e:  # collected and asserted below
            errors.append(e)

    workers = [threading.Thread(target=write, args=(t.id,)) for t in threads]
    for w in workers:
        w.start()
    for w in workers:
        w.join()
    assert not errors, errors
    assert store.count_messages() == 20 * 10 * 2
