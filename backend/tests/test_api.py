"""HTTP API (Phase 4; eval.md §6, EV-4-01 to EV-4-13).

Most tests use a scripted pipeline, so they check the API layer alone. The follow-up and
readiness tests use the real pipeline over the saved Groww pages with a scripted model.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Iterator, Sequence
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from growwbuddy.constants import DISCLAIMER, EXAMPLE_QUESTIONS, SCHEME_KEYS
from growwbuddy.ids import THREAD_ID_PATTERN, new_thread_id
from growwbuddy.main import MAX_BODY_BYTES
from growwbuddy.pipeline import refusals
from growwbuddy.pipeline.orchestrator import Pipeline
from growwbuddy.pipeline.types import Response, Turn
from growwbuddy.retrieval.headers import RetrievalHeaders
from growwbuddy.retrieval.retriever import Retriever
from growwbuddy.retrieval.vector import VectorIndex
from growwbuddy.store.threads import ThreadStore
from tests.api_helpers import (
    CLIENT_A,
    CLIENT_B,
    MID_URL,
    ORIGIN,
    ScriptedPipeline,
    client_for,
    history,
    make_app,
    make_services,
    new_thread,
    send,
)
from tests.fakes import FailIfCalledLLM, FakeEmbedder, FakeLLM, answer_says, router_says

PAN = "ABCDE1234F"


@pytest.fixture
def pipeline() -> ScriptedPipeline:
    return ScriptedPipeline()


@pytest.fixture
def client(session_factory: sessionmaker[Session], pipeline: ScriptedPipeline) -> TestClient:
    return client_for(make_services(session_factory, pipeline))


def count_messages(sf: sessionmaker[Session]) -> int:
    return ThreadStore(sf).count_messages()


# ---------------------------------------------------------------- EV-4-01 contract


def test_EV_4_01_endpoint_contract(client: TestClient) -> None:
    r = client.post("/api/threads", json={"client_id": CLIENT_A})
    assert r.status_code == 201
    t = r.json()
    assert set(t) == {"thread_id", "title", "created_at", "updated_at"}
    assert re.match(THREAD_ID_PATTERN, t["thread_id"]) and t["title"] == "New chat"
    tid = t["thread_id"]

    reply = send(client, tid, "What is the expense ratio of HDFC Mid Cap Fund?")
    assert set(reply) == {
        "message_id", "thread_id", "type", "text", "citation", "last_updated", "footer",
        "thread_title",
    }  # fmt: skip
    assert reply["thread_id"] == tid and reply["type"] == "answer"
    assert reply["message_id"].startswith("msg_")
    assert reply["citation"] == {"url": MID_URL, "title": "HDFC Mid Cap Fund Direct Growth – Groww"}
    assert reply["last_updated"] == "2026-11-10"
    assert reply["footer"] == "Last updated from sources: 10 Nov 2026"

    r = client.get("/api/threads", params={"client_id": CLIENT_A})
    assert r.status_code == 200
    assert [x["thread_id"] for x in r.json()["threads"]] == [tid]

    h = history(client, tid)
    assert h["thread"]["thread_id"] == tid
    user, bot = h["messages"]
    assert user["role"] == "user" and user["type"] is None and user["citation"] is None
    assert user["text"] == "What is the expense ratio of HDFC Mid Cap Fund?"
    assert bot["role"] == "assistant" and bot["message_id"] == reply["message_id"]
    assert {k: bot[k] for k in ("type", "text", "citation", "last_updated", "footer")} == {
        k: reply[k] for k in ("type", "text", "citation", "last_updated", "footer")
    }

    r = client.delete(f"/api/threads/{tid}", params={"client_id": CLIENT_A})
    assert r.status_code == 204 and r.content == b""

    r = client.get("/api/meta")
    assert r.status_code == 200

    r = client.get("/healthz")
    assert r.status_code == 200 and r.json() == {"status": "ok", "ready": True}


def test_request_id_header(client: TestClient) -> None:
    r = client.get("/api/meta")
    assert re.match(r"^req_[0-9a-f]{16}$", r.headers["x-request-id"])
    r = client.get("/api/meta", headers={"X-Request-ID": "trace-abc-12345"})
    assert r.headers["x-request-id"] == "trace-abc-12345"
    r = client.get("/api/meta", headers={"X-Request-ID": "bad id with spaces"})
    assert r.headers["x-request-id"].startswith("req_")


# ---------------------------------------------------------------- EV-4-02 validation


def _assert_rejected(r: Any, *secrets: str) -> None:
    assert r.status_code == 422, r.text
    for s in secrets:
        assert s not in r.text  # never echoed


def test_EV_4_02_input_validation(client: TestClient, pipeline: ScriptedPipeline) -> None:
    tid = new_thread(client)
    url = f"/api/threads/{tid}/messages"

    # Malformed JSON, carrying a PAN.
    bad = '{"client_id": "' + CLIENT_A + '", "text": "my PAN ' + PAN + '"'
    r = client.post(url, content=bad, headers={"content-type": "application/json"})
    _assert_rejected(r, PAN)

    # Wrong types, empty and whitespace text, over-length text.
    _assert_rejected(client.post(url, json={"client_id": CLIENT_A, "text": 42}))
    _assert_rejected(client.post(url, json={"client_id": CLIENT_A, "text": ""}))
    _assert_rejected(client.post(url, json={"client_id": CLIENT_A, "text": " \n\t "}))
    long = "Q" + "z" * 1000  # 1,001 characters
    _assert_rejected(client.post(url, json={"client_id": CLIENT_A, "text": long}), long[:50])
    ok_len = "Q" + "z" * 999  # exactly 1,000 is allowed
    assert client.post(url, json={"client_id": CLIENT_A, "text": ok_len}).status_code == 200

    # Bad client_id in body and query: traversal, SQL text, a PAN.
    for cid in ("../../etc", "1' OR '1'='1", PAN, ""):
        _assert_rejected(client.post(url, json={"client_id": cid, "text": "hi"}), PAN)
        _assert_rejected(client.get("/api/threads", params={"client_id": cid}), PAN)
    _assert_rejected(client.get("/api/threads"))  # missing

    # Bad thread_id in the path.
    for bad_tid in ("1' OR '1'='1", PAN, "thr_" + "0" * 25, tid.lower()):
        r = client.post(f"/api/threads/{bad_tid}/messages", json={"client_id": CLIENT_A,
                                                                  "text": "hi"})  # fmt: skip
        _assert_rejected(r, PAN)
    r = client.get("/api/threads/..%2F..%2Fetc/messages", params={"client_id": CLIENT_A})
    assert r.status_code in (404, 422)

    # Only the one valid message reached the pipeline; nothing else was stored.
    assert [c[0] for c in pipeline.calls] == [ok_len]


def test_non_json_body_is_rejected(client: TestClient) -> None:
    # A cross-site form can only send "simple" content types; the API takes JSON only.
    tid = new_thread(client)
    body = '{"client_id": "' + CLIENT_A + '", "text": "hi"}'
    r = client.post(
        f"/api/threads/{tid}/messages", content=body, headers={"content-type": "text/plain"}
    )
    assert r.status_code == 422


# ---------------------------------------------------------------- EV-4-03 body limit


def test_EV_4_03_body_size_limit(client: TestClient) -> None:
    tid = new_thread(client)
    url = f"/api/threads/{tid}/messages"
    big = '{"client_id": "' + CLIENT_A + '", "text": "' + "x" * (1024 * 1024) + '"}'
    r = client.post(url, content=big, headers={"content-type": "application/json"})
    assert r.status_code == 413
    assert len(r.content) < 200

    def chunks() -> Iterator[bytes]:  # no Content-Length: counted as it streams
        for _ in range(64):
            yield b"x" * 1024

    r = client.post(url, content=chunks(), headers={"content-type": "application/json"})
    assert r.status_code == 413
    assert len("x" * MAX_BODY_BYTES) == MAX_BODY_BYTES  # limit is a plain byte count


# ---------------------------------------------------------------- EV-4-05 ordering


async def test_EV_4_05_same_thread_ordering(session_factory: sessionmaker[Session]) -> None:
    import httpx

    async def slow_first(text: str) -> None:
        await asyncio.sleep(0.05 if text == "first" else 0)

    svc = make_services(session_factory, ScriptedPipeline(before=slow_first))
    transport = httpx.ASGITransport(app=make_app(svc))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        tid = (await ac.post("/api/threads", json={"client_id": CLIENT_A})).json()["thread_id"]
        url = f"/api/threads/{tid}/messages"
        r1, r2 = await asyncio.gather(
            ac.post(url, json={"client_id": CLIENT_A, "text": "first"}),
            ac.post(url, json={"client_id": CLIENT_A, "text": "second"}),
        )
        assert r1.status_code == r2.status_code == 200
        msgs = (await ac.get(url, params={"client_id": CLIENT_A})).json()["messages"]
    assert [m["text"] for m in msgs] == [
        "first", "Reply to: first", "second", "Reply to: second",
    ]  # fmt: skip


# ---------------------------------------------------------------- EV-4-06 / 07 PII


@pytest.mark.parametrize(
    "text",
    [f"My PAN is {PAN}, what is the exit load?", "call me on +91 98765 43210", "a.b@example.com"],
)
def test_EV_4_06_pii_not_stored(
    session_factory: sessionmaker[Session], client: TestClient, pipeline: ScriptedPipeline,
    text: str,
) -> None:  # fmt: skip
    tid = new_thread(client)
    before = count_messages(session_factory)
    reply = send(client, tid, text)
    assert reply["type"] == "pii_rejected" and reply["text"] == refusals.PII
    assert reply["message_id"] is None
    assert reply["citation"] is None and reply["footer"] is None
    assert reply["thread_title"] == "New chat"
    assert count_messages(session_factory) == before
    assert history(client, tid)["thread"]["title"] == "New chat"
    assert pipeline.calls == []  # never reached the pipeline (or the model)


def test_EV_4_07_follow_up_skips_rejected_turn(
    indexed: sessionmaker[Session], retrieval_headers: RetrievalHeaders
) -> None:
    ter = "The expense ratio (TER) of HDFC Mid Cap Fund Direct Growth is 0.74% as on 10 Nov 2026."
    el = "HDFC Mid Cap Fund Direct Growth has an exit load of 1% if redeemed within 1 year."
    llm = FakeLLM(
        router_says("hdfc-mid-cap", query="What is the expense ratio of HDFC Mid Cap Fund?"),
        answer_says(ter, cite="fact:hdfc-mid-cap:expense_ratio_direct"),
        router_says("hdfc-mid-cap", query="What is the exit load of HDFC Mid Cap Fund?"),
        answer_says(el, cite="fact:hdfc-mid-cap:exit_load"),
    )
    emb = FakeEmbedder()
    retriever = Retriever(VectorIndex(indexed, emb.model_name), emb, indexed)
    real = Pipeline(llm, retriever, headers=retrieval_headers)
    client = client_for(
        make_services(indexed, real, ready=retriever.ready, newest=retriever.newest_date)
    )
    tid = new_thread(client)

    first = send(client, tid, "What is the expense ratio of HDFC Mid Cap Fund?")
    assert first["type"] == "answer" and first["text"] == ter
    assert send(client, tid, f"my PAN is {PAN}, and the small cap one?")["type"] == "pii_rejected"
    follow = send(client, tid, "And its exit load?")

    assert follow["type"] == "answer" and follow["citation"]["url"] == MID_URL
    router_prompt = llm.calls[2]["user"]
    assert "What is the expense ratio of HDFC Mid Cap Fund?" in router_prompt
    assert PAN not in router_prompt and "small cap one" not in router_prompt
    assert not llm.results
    assert len(history(client, tid)["messages"]) == 4
    assert (
        history(client, tid)["thread"]["title"] == "What is the expense ratio of HDFC Mid Cap Fund?"
    )


# ---------------------------------------------------------------- EV-4-08 / 09


def test_EV_4_08_delete(session_factory: sessionmaker[Session], client: TestClient) -> None:
    tid = new_thread(client)
    keep = new_thread(client)
    send(client, tid, "q1")
    send(client, keep, "q2")
    assert client.delete(f"/api/threads/{tid}", params={"client_id": CLIENT_A}).status_code == 204
    r = client.get(f"/api/threads/{tid}/messages", params={"client_id": CLIENT_A})
    assert r.status_code == 404
    r = client.post(f"/api/threads/{tid}/messages", json={"client_id": CLIENT_A, "text": "q"})
    assert r.status_code == 404
    assert client.delete(f"/api/threads/{tid}", params={"client_id": CLIENT_A}).status_code == 404
    assert ThreadStore(session_factory).count_messages(tid) == 0
    assert len(history(client, keep)["messages"]) == 2
    threads = client.get("/api/threads", params={"client_id": CLIENT_A}).json()["threads"]
    assert [t["thread_id"] for t in threads] == [keep]


def test_EV_4_09_ownership(
    session_factory: sessionmaker[Session], client: TestClient, pipeline: ScriptedPipeline
) -> None:
    tid = new_thread(client, CLIENT_A)
    send(client, tid, "q1")
    r = client.get(f"/api/threads/{tid}/messages", params={"client_id": CLIENT_B})
    assert r.status_code == 404
    r = client.post(f"/api/threads/{tid}/messages", json={"client_id": CLIENT_B, "text": "q"})
    assert r.status_code == 404
    r = client.delete(f"/api/threads/{tid}", params={"client_id": CLIENT_B})
    assert r.status_code == 404
    assert client.get("/api/threads", params={"client_id": CLIENT_B}).json() == {"threads": []}
    # Foreign and missing threads look the same.
    missing = new_thread_id()
    r2 = client.get(f"/api/threads/{missing}/messages", params={"client_id": CLIENT_A})
    assert r2.status_code == 404 and r2.json() == r.json()
    # The owner's thread is untouched.
    assert len(history(client, tid, CLIENT_A)["messages"]) == 2
    assert len(pipeline.calls) == 1


# ---------------------------------------------------------------- EV-4-10 title


def test_EV_4_10_thread_title(client: TestClient) -> None:
    tid = new_thread(client)
    assert send(client, tid, "hi")["thread_title"] == "New chat"
    assert history(client, tid)["thread"]["title"] == "New chat"
    q = "What is the exit load of HDFC Small Cap Fund Direct Growth if I redeem in 6 months?"
    assert len(q) > 80 - 5
    reply = send(client, tid, q)
    assert reply["thread_title"] == q[:60]
    send(client, tid, "and the benchmark?")
    listed = client.get("/api/threads", params={"client_id": CLIENT_A}).json()["threads"]
    assert listed[0]["title"] == q[:60]


# ---------------------------------------------------------------- EV-4-11 meta


def test_EV_4_11_meta(client: TestClient) -> None:
    m = client.get("/api/meta").json()
    assert m["welcome"] == refusals.WELCOME
    assert m["example_questions"] == list(EXAMPLE_QUESTIONS) and len(m["example_questions"]) == 3
    assert m["disclaimer"] == DISCLAIMER == "Facts-only. No investment advice."
    assert "PAN" in m["input_hint"]
    assert m["last_updated"] == "2026-11-11"
    assert m["footer"] == "Last updated from sources: 11 Nov 2026"
    assert [s["key"] for s in m["schemes"]] == list(SCHEME_KEYS)
    assert all(s["url"].startswith("https://groww.in/") for s in m["schemes"])


def test_meta_from_the_real_index(indexed: sessionmaker[Session]) -> None:
    emb = FakeEmbedder()
    retriever = Retriever(VectorIndex(indexed, emb.model_name), emb, indexed)
    client = client_for(make_services(indexed, newest=retriever.newest_date))
    assert client.get("/api/meta").json()["last_updated"] == "2026-11-11"


# ---------------------------------------------------------------- EV-4-12 CORS


def test_EV_4_12_cors(client: TestClient) -> None:
    pre = {
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "content-type",
    }
    r = client.options("/api/threads", headers={"Origin": ORIGIN, **pre})
    assert r.status_code == 200
    assert r.headers["access-control-allow-origin"] == ORIGIN

    evil = "https://evil.example"
    r = client.options("/api/threads", headers={"Origin": evil, **pre})
    assert r.status_code == 400
    assert "access-control-allow-origin" not in r.headers
    r = client.get("/api/threads", params={"client_id": CLIENT_A}, headers={"Origin": evil})
    assert "access-control-allow-origin" not in r.headers  # the browser blocks the read
    r = client.get("/api/meta", headers={"Origin": ORIGIN})
    assert r.headers["access-control-allow-origin"] == ORIGIN


# ---------------------------------------------------------------- EV-4-13 readiness


def test_EV_4_13_readiness_with_empty_index(session_factory: sessionmaker[Session]) -> None:
    emb = FakeEmbedder()
    retriever = Retriever(VectorIndex(session_factory, emb.model_name), emb, session_factory)
    llm = FakeLLM(router_says("hdfc-mid-cap", query="What is the exit load of HDFC Mid Cap?"))
    svc = make_services(
        session_factory, Pipeline(llm, retriever), ready=retriever.ready,
        newest=retriever.newest_date,
    )  # fmt: skip
    with client_for(svc) as client:  # runs startup too
        assert client.get("/healthz").json() == {"status": "ok", "ready": False}
        assert client.get("/api/meta").json()["last_updated"] is None
        tid = new_thread(client)
        reply = send(client, tid, "What is the exit load of HDFC Mid Cap Fund?")
        assert reply["type"] == "unavailable" and reply["text"] == refusals.UNAVAILABLE
        assert reply["citation"] is None


def test_healthz_ready_with_index(indexed: sessionmaker[Session]) -> None:
    emb = FakeEmbedder()
    retriever = Retriever(VectorIndex(indexed, emb.model_name), emb, indexed)
    client = client_for(make_services(indexed, Pipeline(FailIfCalledLLM(), retriever),
                                      ready=retriever.ready))  # fmt: skip
    assert client.get("/healthz").json() == {"status": "ok", "ready": True}


# ---------------------------------------------------------------- other edge cases


def test_EC_THR_07_duplicate_send_returns_stored_reply(
    session_factory: sessionmaker[Session], client: TestClient, pipeline: ScriptedPipeline
) -> None:
    tid = new_thread(client)
    cmid = "11111111-2222-4333-8444-555555555555"
    a = send(client, tid, "TER of mid cap?", client_message_id=cmid)
    b = send(client, tid, "TER of mid cap?", client_message_id=cmid)
    assert a == b
    assert len(pipeline.calls) == 1
    assert ThreadStore(session_factory).count_messages(tid) == 2
    # The same client_message_id in another thread is a separate message.
    other = new_thread(client)
    assert (
        send(client, other, "TER of mid cap?", client_message_id=cmid)["message_id"]
        != (a["message_id"])
    )


async def test_EC_THR_09_reply_discarded_if_thread_deleted(
    session_factory: sessionmaker[Session],
) -> None:
    import httpx

    started, release = asyncio.Event(), asyncio.Event()

    async def block(_text: str) -> None:
        started.set()
        await release.wait()

    svc = make_services(session_factory, ScriptedPipeline(before=block))
    transport = httpx.ASGITransport(app=make_app(svc))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        tid = (await ac.post("/api/threads", json={"client_id": CLIENT_A})).json()["thread_id"]
        pending = asyncio.create_task(
            ac.post(f"/api/threads/{tid}/messages", json={"client_id": CLIENT_A, "text": "q"})
        )
        await started.wait()
        r = await ac.delete(f"/api/threads/{tid}", params={"client_id": CLIENT_A})
        assert r.status_code == 204  # delete doesn't wait for the reply
        release.set()
        assert (await pending).status_code == 404
    assert ThreadStore(session_factory).count_messages() == 0


def test_pipeline_crash_gives_unavailable_not_500(session_factory: sessionmaker[Session]) -> None:
    def boom(_text: str, _h: Sequence[Turn]) -> Response:
        raise RuntimeError("something broke")

    client = client_for(make_services(session_factory, ScriptedPipeline(respond=boom)))
    tid = new_thread(client)
    reply = send(client, tid, "TER of mid cap?")
    assert reply["type"] == "unavailable" and reply["text"] == refusals.UNAVAILABLE


def test_busy_reply_not_stored(session_factory: sessionmaker[Session]) -> None:
    def busy(_text: str, _h: Sequence[Turn]) -> Response:
        return Response(type="busy", text=refusals.BUSY, reason="rate_limited")

    client = client_for(make_services(session_factory, ScriptedPipeline(respond=busy)))
    tid = new_thread(client)
    reply = send(client, tid, "TER of mid cap?")
    assert reply["type"] == "busy" and reply["message_id"] is None
    assert reply["citation"] is None and reply["footer"] is None
    assert history(client, tid)["messages"] == []


def test_history_passed_to_pipeline_is_this_threads_last_turns(
    session_factory: sessionmaker[Session], pipeline: ScriptedPipeline
) -> None:
    client = client_for(make_services(session_factory, pipeline, history_turns=2))
    a, b = new_thread(client), new_thread(client)
    for i in range(3):
        send(client, a, f"a{i}")
        send(client, b, f"b{i}")
    send(client, a, "a-last")
    text, hist = pipeline.calls[-1]
    assert text == "a-last"
    assert [t.user_text for t in hist] == ["a1", "a2"]
    assert hist[-1].scheme == "hdfc-mid-cap" and hist[-1].response_text == "Reply to: a2"
