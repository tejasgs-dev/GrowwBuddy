"""ASGI middleware: request IDs with a safe access log, and a request body size limit.

Pure ASGI rather than `BaseHTTPMiddleware`, so context variables set here are seen by the
handlers and everything they call.
"""

from __future__ import annotations

import json
import logging
import re
import secrets
import time
from typing import Any

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from growwbuddy.obs import request_context

log = logging.getLogger("growwbuddy.api.access")

REQUEST_ID_HEADER = "x-request-id"
_SAFE_REQUEST_ID = re.compile(r"^[A-Za-z0-9_-]{8,64}$")


def new_request_id() -> str:
    return "req_" + secrets.token_hex(8)


class RequestContextMiddleware:
    """Gives each request an ID, returns it in `X-Request-ID`, and logs one access line.

    The access line has the method, the **route template** (e.g.
    `/api/threads/{thread_id}/messages`), the status and the latency. It never has the raw
    path or query string: an invalid path could hold anything a user typed (EC-API-09).
    An incoming `X-Request-ID` is reused only if it is a plain token.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        incoming = _header(scope, REQUEST_ID_HEADER)
        rid = incoming if incoming and _SAFE_REQUEST_ID.match(incoming) else new_request_id()
        status = 500
        started = time.monotonic()

        async def send_wrapper(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                headers = list(message.get("headers", []))
                headers.append((REQUEST_ID_HEADER.encode(), rid.encode()))
                message = {**message, "headers": headers}
            await send(message)

        with request_context(rid):
            try:
                await self.app(scope, receive, send_wrapper)
            finally:
                route = scope.get("route")
                log.info(
                    "request method=%s route=%s status=%d latency_ms=%d",
                    scope.get("method"),
                    getattr(route, "path", None) or "unmatched",
                    status,
                    round(1000 * (time.monotonic() - started)),
                )


class BodySizeLimitMiddleware:
    """Rejects request bodies over `max_bytes` with 413 (EC-API-02).

    Checks `Content-Length` up front, then reads the body (bodies here are a few hundred
    bytes) counting as it goes, so a chunked body or a false header doesn't get around the
    limit. The app then gets the buffered body as one message.
    """

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        length = _header(scope, "content-length")
        if length is not None and (not length.isdigit() or int(length) > self.max_bytes):
            await _too_large(send)
            return

        chunks: list[bytes] = []
        seen = 0
        while True:
            message = await receive()
            if message["type"] != "http.request":  # client went away
                return
            body = message.get("body", b"")
            seen += len(body)
            if seen > self.max_bytes:
                await _too_large(send)
                return
            chunks.append(body)
            if not message.get("more_body", False):
                break

        replayed = False

        async def replay() -> Message:
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": b"".join(chunks), "more_body": False}
            return await receive()  # after the body: wait for disconnect as usual

        await self.app(scope, replay, send)


def _header(scope: Scope, name: str) -> str | None:
    key = name.encode()
    for k, v in scope.get("headers", []):
        if k.lower() == key:
            value: str = v.decode("latin-1")
            return value
    return None


async def _too_large(send: Send) -> None:
    body = json.dumps({"detail": "request body too large"}).encode()
    headers: list[tuple[bytes, bytes]] = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(body)).encode()),
    ]
    start: dict[str, Any] = {"type": "http.response.start", "status": 413, "headers": headers}
    await send(start)
    await send({"type": "http.response.body", "body": body})
