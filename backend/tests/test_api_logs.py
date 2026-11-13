"""EV-4-15 (blocking): a full API session logs no message text (§11, EC-API-09), and the
summary lines carry the request ID, thread ID, intent, latency, tokens and validator result.
"""

from __future__ import annotations

import json
import logging
import sys
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.orm import Session, sessionmaker

from growwbuddy.config import Settings
from growwbuddy.llm.client import GroqClient, _Attempt
from growwbuddy.obs import JsonFormatter, record_llm_call, request_context
from growwbuddy.pipeline.orchestrator import Pipeline
from growwbuddy.retrieval.headers import RetrievalHeaders
from growwbuddy.retrieval.retriever import Retriever
from growwbuddy.retrieval.vector import VectorIndex
from tests.api_helpers import CLIENT_A, client_for, make_services, new_thread, send
from tests.fakes import FakeEmbedder, FakeLLM, answer_says, router_says

PAN = "ABCDE1234F"
TER = "The expense ratio (TER) of HDFC Mid Cap Fund Direct Growth is 0.74% as on 10 Nov 2026."
EXIT_LOAD = "HDFC Mid Cap Fund Direct Growth has an exit load of 1% if redeemed within 1 year."

# Every user message sent in the session; none may appear in any log line.
MESSAGES = [
    "What is the expense ratio of HDFC Mid Cap Fund?",
    f"My PAN is {PAN}, what is the exit load?",
    "Should I invest in HDFC Small Cap Fund right now?",
    "What's the NAV of HDFC large cap mutual fund?",
    "Thanks so much!",
    "And what about its exit load please?",
]
OVERLONG = "OVERLONGMARKER " + "z" * 1000


def server_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    # httpx is the *test client* logging its own request URLs, not the server.
    return [r for r in caplog.records if not r.name.startswith("httpx")]


def test_EV_4_15_no_message_text_in_logs(
    caplog: pytest.LogCaptureFixture,
    indexed: sessionmaker[Session],
    retrieval_headers: RetrievalHeaders,
) -> None:
    caplog.set_level(logging.DEBUG)
    llm = FakeLLM(
        router_says("hdfc-mid-cap", query="What is the expense ratio of HDFC Mid Cap Fund?"),
        answer_says(TER, cite="fact:hdfc-mid-cap:expense_ratio_direct"),
        router_says("hdfc-mid-cap", query="What is the exit load of HDFC Mid Cap Fund?"),
        answer_says(EXIT_LOAD, cite="fact:hdfc-mid-cap:exit_load"),
    )
    emb = FakeEmbedder()
    retriever = Retriever(VectorIndex(indexed, emb.model_name), emb, indexed)
    pipeline = Pipeline(llm, retriever, headers=retrieval_headers)
    svc = make_services(indexed, pipeline, ready=retriever.ready, newest=retriever.newest_date)

    with client_for(svc) as client:
        tid = new_thread(client)
        types = [send(client, tid, m)["type"] for m in MESSAGES]
        assert types == ["answer", "pii_rejected", "refusal", "performance", "greeting", "answer"]
        url = f"/api/threads/{tid}/messages"
        assert client.post(url, json={"client_id": CLIENT_A, "text": OVERLONG}).status_code == 422
        bad = client.post(f"/api/threads/{PAN}/messages", json={"client_id": CLIENT_A,
                                                                "text": MESSAGES[0]})  # fmt: skip
        assert bad.status_code == 422
        client.get(f"/api/threads/{tid}/messages", params={"client_id": CLIENT_A})
    assert not llm.results

    records = server_records(caplog)
    fmt = JsonFormatter()
    lines = [r.getMessage() for r in records] + [fmt.format(r) for r in records]
    forbidden = [*MESSAGES, PAN, "OVERLONGMARKER", TER, EXIT_LOAD]
    leaks = [(s, line) for s in forbidden for line in lines if s.lower() in line.lower()]
    assert leaks == []

    # The summary line per message has what §11 asks for.
    done = [r for r in records if r.getMessage().startswith("message done")]
    assert len(done) == len(MESSAGES)
    for r in done:
        assert getattr(r, "request_id", "").startswith("req_")
        assert getattr(r, "thread_id", None) == tid
        msg = r.getMessage()
        for key in ("type=", "reason=", "stage=", "validator=", "tokens=", "latency_ms="):
            assert key in msg
    first = done[0].getMessage()
    assert "type=answer" in first and "validator=pass" in first and "stage=answer" in first
    assert "stage=pii" in done[1].getMessage()
    assert "stage=rules:advisory" in done[2].getMessage()

    # Access lines use the route template, never the raw path.
    access = [r.getMessage() for r in records if r.name == "growwbuddy.api.access"]
    assert any("route=/api/threads/{thread_id}/messages status=200" in a for a in access)
    assert all(tid not in a for a in access)


def test_json_formatter_shape_and_no_traceback() -> None:
    with request_context("req_0123456789abcdef"):
        try:
            raise ValueError("user text inside an exception")
        except ValueError:
            record = logging.getLogger("t").makeRecord(
                "t", logging.ERROR, __file__, 1, "failed kind=%s", ("x",), sys.exc_info()
            )
    line = JsonFormatter().format(record)
    entry = json.loads(line)
    assert entry["msg"] == "failed kind=x" and entry["level"] == "ERROR"
    assert entry["request_id"] == "req_0123456789abcdef"
    assert entry["exc_type"] == "ValueError"
    assert "user text" not in line


def completion() -> Any:
    msg = SimpleNamespace(content='{"a": 1}', refusal=None, reasoning="hidden")
    usage = SimpleNamespace(
        prompt_tokens=200, completion_tokens=50,
        prompt_tokens_details=SimpleNamespace(cached_tokens=120),
    )  # fmt: skip
    return SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop", message=msg)],
                           usage=usage)  # fmt: skip


async def test_groq_usage_adds_up_per_request() -> None:
    async def send(_request: dict[str, Any]) -> _Attempt:
        return _Attempt(completion=completion(), headers={})

    client = GroqClient(Settings(_env_file=None), send=send)
    with request_context("req_0123456789abcdef") as stats:
        for kind in ("router", "answer"):
            await client.complete_json(system="s", user="u", schema_name="x",
                                       schema={"type": "object"}, max_tokens=10,
                                       kind=kind)  # fmt: skip
    assert stats.llm_calls == 2
    assert stats.total_tokens == 500 and stats.cached_tokens == 240
    # Outside a request nothing is recorded, and nothing breaks.
    record_llm_call({"total_tokens": 5})
