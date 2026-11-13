"""Helpers for the Phase 4 API tests: a scripted pipeline and app/client builders."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from datetime import date
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from growwbuddy.api.locks import ThreadLocks
from growwbuddy.api.service import MessageHandler, Services
from growwbuddy.config import Settings
from growwbuddy.constants import SOURCES, format_footer
from growwbuddy.main import create_app
from growwbuddy.pipeline.types import Citation, Response, Turn
from growwbuddy.store.threads import ThreadStore

ORIGIN = "http://localhost:5173"
CLIENT_A = "0b6f6d3e-6f5c-4a43-9d3a-2f6c1d1e9a01"
CLIENT_B = "7c1e2d4f-1a2b-4c3d-8e9f-0a1b2c3d4e5f"
CORPUS_DATE = date(2026, 11, 11)
TER_DATE = date(2026, 11, 10)
MID_URL = next(u for _, k, u in SOURCES if k == "hdfc-mid-cap")


def answer(text: str, scheme: str = "hdfc-mid-cap") -> Response:
    return Response(
        type="answer",
        text=text,
        citation=Citation(url=MID_URL, title="HDFC Mid Cap Fund Direct Growth – Groww"),
        last_updated=TER_DATE,
        footer=format_footer(TER_DATE),
        scheme=scheme,
    )


def default_respond(text: str, _history: Sequence[Turn]) -> Response:
    if text.strip().lower() in {"hi", "hello"}:
        return Response(type="greeting", text="Hi! Ask me a factual question.", reason="greeting")
    return answer(f"Reply to: {text}")


class ScriptedPipeline:
    """Stands in for the real pipeline. Records each call's text and history.

    `respond` builds the reply; `before` is awaited first (to add delays or block).
    """

    def __init__(
        self,
        respond: Callable[[str, Sequence[Turn]], Response] = default_respond,
        before: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        self.respond = respond
        self.before = before
        self.calls: list[tuple[str, list[Turn]]] = []

    async def handle_message(self, text: str, history: Sequence[Turn] = ()) -> Response:
        self.calls.append((text, list(history)))
        if self.before is not None:
            await self.before(text)
        await asyncio.sleep(0)
        return self.respond(text, history)


def make_services(
    sf: sessionmaker[Session],
    pipeline: MessageHandler | None = None,
    *,
    ready: Callable[[], bool] = lambda: True,
    newest: Callable[[], date | None] = lambda: CORPUS_DATE,
    locks: ThreadLocks | None = None,
    history_turns: int = 6,
) -> Services:
    return Services(
        store=ThreadStore(sf),
        pipeline=pipeline or ScriptedPipeline(),
        ready=ready,
        newest_date=newest,
        history_turns=history_turns,
        locks=locks or ThreadLocks(),
    )


def make_app(svc: Services) -> FastAPI:
    return create_app(svc, s=Settings(_env_file=None, frontend_origin=ORIGIN))


def client_for(svc: Services) -> TestClient:
    return TestClient(make_app(svc))


def new_thread(client: TestClient, client_id: str = CLIENT_A) -> str:
    r = client.post("/api/threads", json={"client_id": client_id})
    assert r.status_code == 201, r.text
    tid: str = r.json()["thread_id"]
    return tid


def send(
    client: TestClient, thread_id: str, text: str, client_id: str = CLIENT_A, **extra: Any
) -> dict[str, Any]:
    r = client.post(
        f"/api/threads/{thread_id}/messages",
        json={"client_id": client_id, "text": text, **extra},
    )
    assert r.status_code == 200, r.text
    body: dict[str, Any] = r.json()
    return body


def history(client: TestClient, thread_id: str, client_id: str = CLIENT_A) -> dict[str, Any]:
    r = client.get(f"/api/threads/{thread_id}/messages", params={"client_id": client_id})
    assert r.status_code == 200, r.text
    body: dict[str, Any] = r.json()
    return body
