"""EV-4-04 (blocking): 20 threads x 5 messages in parallel stay isolated and in order (§12.3)."""

from __future__ import annotations

import asyncio
import hashlib

import httpx
from sqlalchemy.orm import Session, sessionmaker

from tests.api_helpers import CLIENT_A, CLIENT_B, ScriptedPipeline, make_app, make_services

THREADS = 20
PER_THREAD = 5


async def jitter(text: str) -> None:
    # 0-9 ms, fixed per message, so replies finish out of send order across threads.
    await asyncio.sleep(hashlib.md5(text.encode()).digest()[0] % 10 / 1000)


async def test_EV_4_04_concurrent_threads_are_isolated(
    session_factory: sessionmaker[Session],
) -> None:
    pipeline = ScriptedPipeline(before=jitter)
    app = make_app(make_services(session_factory, pipeline))
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:

        async def conversation(n: int) -> tuple[str, str]:
            client_id = CLIENT_A if n % 2 == 0 else CLIENT_B
            r = await ac.post("/api/threads", json={"client_id": client_id})
            tid: str = r.json()["thread_id"]
            for i in range(PER_THREAD):
                r = await ac.post(
                    f"/api/threads/{tid}/messages",
                    json={"client_id": client_id, "text": f"t{n}-m{i}"},
                )
                assert r.status_code == 200, r.text
                assert r.json()["text"] == f"Reply to: t{n}-m{i}"
            return tid, client_id

        threads = await asyncio.gather(*(conversation(n) for n in range(THREADS)))

        for n, (tid, client_id) in enumerate(threads):
            r = await ac.get(f"/api/threads/{tid}/messages", params={"client_id": client_id})
            texts = [m["text"] for m in r.json()["messages"]]
            expected = []
            for i in range(PER_THREAD):
                expected += [f"t{n}-m{i}", f"Reply to: t{n}-m{i}"]
            assert texts == expected, f"thread {n} holds {texts}"

        listed_a = (await ac.get("/api/threads", params={"client_id": CLIENT_A})).json()
        assert len(listed_a["threads"]) == THREADS // 2

    # Every pipeline call saw only its own thread's earlier turns, in order.
    assert len(pipeline.calls) == THREADS * PER_THREAD
    for text, hist in pipeline.calls:
        n_str, i_str = text[1:].split("-m")
        assert [t.user_text for t in hist] == [f"t{n_str}-m{j}" for j in range(int(i_str))][-6:]
