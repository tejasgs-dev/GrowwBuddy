"""Per-thread lock registry (task 4.3; EV-4-05, EV-4-14)."""

from __future__ import annotations

import asyncio

from growwbuddy.api.locks import ThreadLocks


class FakeClock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


async def test_same_thread_runs_one_at_a_time_in_arrival_order() -> None:
    locks = ThreadLocks()
    order: list[str] = []
    active = 0
    peak = 0

    async def job(name: str, delay: float) -> None:
        nonlocal active, peak
        async with locks.hold("thr_a"):
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(delay)
            order.append(name)
            active -= 1

    # The first job is slowest: without the lock it would finish last.
    await asyncio.gather(job("1", 0.03), job("2", 0.01), job("3", 0.0))
    assert order == ["1", "2", "3"]
    assert peak == 1


async def test_different_threads_do_not_block_each_other() -> None:
    locks = ThreadLocks()
    release = asyncio.Event()
    entered_b = asyncio.Event()

    async def hold_a() -> None:
        async with locks.hold("thr_a"):
            await release.wait()

    async def hold_b() -> None:
        async with locks.hold("thr_b"):
            entered_b.set()

    task = asyncio.create_task(hold_a())
    await asyncio.sleep(0)
    await asyncio.wait_for(hold_b(), timeout=1)
    assert entered_b.is_set()
    release.set()
    await task


async def test_EV_4_14_idle_locks_are_swept() -> None:
    clock = FakeClock()
    locks = ThreadLocks(idle_s=600, sweep_every_s=60, clock=clock)
    for i in range(1000):
        async with locks.hold(f"thr_{i}"):
            pass
    assert len(locks) == 1000
    clock.t = 601
    async with locks.hold("thr_new"):  # any later use triggers the sweep
        pass
    assert len(locks) == 1  # only the one just used


async def test_held_or_awaited_lock_is_never_swept() -> None:
    clock = FakeClock()
    locks = ThreadLocks(idle_s=10, clock=clock)
    release = asyncio.Event()

    async def hold() -> None:
        async with locks.hold("thr_busy"):
            await release.wait()

    task = asyncio.create_task(hold())
    await asyncio.sleep(0)
    clock.t = 1000
    assert locks.sweep() == 0
    assert len(locks) == 1
    release.set()
    await task
    clock.t = 2000
    assert locks.sweep() == 1
