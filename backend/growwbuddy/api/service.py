"""What the routes run: the send-message flow and the services it needs (tasks 4.2-4.8).

Sending a message, in order:

1. Take the thread's lock, so messages in one thread are handled in send order (4.3).
2. Check the thread exists and belongs to the caller (EC-THR-11).
3. A repeat of an earlier send (same `client_message_id`) gets the stored reply (EC-THR-07).
4. PII guard, **before** history is read or anything is written (4.5, P6).
4b. Per-client and per-IP limits, only for messages that need the model (6.4). Over the
   limit gets the busy reply, which isn't stored, so the user can send it again later.
5. Load the last N turns from the store; there is no in-memory session state (§9.2).
6. Run the pipeline. It never raises by design; if it does anyway, the user gets the
   "unavailable" reply, not a 500.
7. Store the message and its reply in one transaction, unless the reply type isn't stored
   (PII, busy) or the thread was deleted meanwhile (EC-THR-09).
8. Log one summary line: IDs, type, reason, stage, validator result, Groq calls and
   tokens, latency. Never the text (4.8, §11).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Protocol

from growwbuddy.api.limits import MessageLimiter
from growwbuddy.api.locks import ThreadLocks
from growwbuddy.obs import current_stats, note, set_thread_id
from growwbuddy.pipeline import refusals
from growwbuddy.pipeline.orchestrator import needs_model, should_store
from growwbuddy.pipeline.pii_guard import check as pii_check
from growwbuddy.pipeline.types import Citation, Response, Turn
from growwbuddy.store.threads import StoredMessage, ThreadInfo, ThreadStore

log = logging.getLogger(__name__)


class MessageHandler(Protocol):
    async def handle_message(self, text: str, history: Sequence[Turn] = ()) -> Response: ...


@dataclass
class Services:
    store: ThreadStore
    pipeline: MessageHandler
    ready: Callable[[], bool]
    newest_date: Callable[[], date | None]
    history_turns: int = 6
    locks: ThreadLocks = field(default_factory=ThreadLocks)
    # Per-client/IP limits on model-answered messages (6.4); None means no limit.
    limiter: MessageLimiter | None = None
    needs_model: Callable[[str], bool] = needs_model


@dataclass(frozen=True)
class SendResult:
    thread: ThreadInfo
    response: Response
    message_id: str | None  # None when the reply wasn't stored


class ThreadNotFoundError(LookupError):
    pass


def response_from_stored(m: StoredMessage) -> Response:
    citation = (
        Citation(url=m.citation_url, title=m.citation_title or "") if m.citation_url else None
    )
    return Response(
        type=m.response_type,  # type: ignore[arg-type]  # only valid types are stored
        text=m.content,
        citation=citation,
        last_updated=m.as_of_date,
        footer=m.footer,
        reason=m.reason,
        scheme=m.scheme,
    )


async def send_message(
    svc: Services,
    thread_id: str,
    client_id: str,
    text: str,
    client_message_id: str | None = None,
    client_ip: str | None = None,
) -> SendResult:
    started = time.monotonic()
    set_thread_id(thread_id)
    async with svc.locks.hold(thread_id):
        thread = await asyncio.to_thread(svc.store.get, thread_id, client_id)
        if thread is None:
            raise ThreadNotFoundError(thread_id)

        if client_message_id is not None:
            earlier = await asyncio.to_thread(svc.store.find_reply, thread_id, client_message_id)
            if earlier is not None:
                note(stage="duplicate")
                log.info("duplicate send; returning stored reply message_id=%s", earlier.id)
                return SendResult(thread, response_from_stored(earlier), earlier.id)

        pii = pii_check(text)
        if pii.found:
            note(stage="pii")
            log.info("pii rejected types=%s", ",".join(pii.types))
            resp = Response(type="pii_rejected", text=refusals.PII, reason="pii")
        elif (limited := _over_limit(svc, client_id, client_ip, text)) is not None:
            resp = limited
        else:
            history = await asyncio.to_thread(svc.store.recent_turns, thread_id, svc.history_turns)
            resp = await _run_pipeline(svc.pipeline, text, history)

        message_id: str | None = None
        if should_store(resp):
            saved = await asyncio.to_thread(
                svc.store.append_exchange,
                thread_id,
                text,
                resp,
                client_message_id=client_message_id,
            )
            if saved is None:
                log.info("thread deleted during reply; reply discarded")
                raise ThreadNotFoundError(thread_id)
            reply, thread = saved
            message_id = reply.id

    _log_summary(resp, stored=message_id is not None, started=started)
    return SendResult(thread, resp, message_id)


def _over_limit(svc: Services, client_id: str, client_ip: str | None, text: str) -> Response | None:
    """The busy reply if this model-bound message breaks a per-client or per-IP limit."""
    if svc.limiter is None or not svc.needs_model(text):
        return None
    check = svc.limiter.try_acquire(client_id, client_ip)
    if check.ok:
        return None
    note(stage="client_limit")
    log.info(
        "message limit reached scope=%s retry_after_s=%s",
        check.scope,
        None if check.retry_after_s is None else round(check.retry_after_s),
    )
    return Response(type="busy", text=refusals.BUSY, reason="client_limit")


async def _run_pipeline(pipeline: MessageHandler, text: str, history: list[Turn]) -> Response:
    try:
        return await pipeline.handle_message(text, history)
    except Exception as e:
        # Type only: an exception message could quote the user's text.
        log.error("pipeline raised error=%s", type(e).__name__)
        return Response(type="unavailable", text=refusals.UNAVAILABLE, reason="internal_error")


def _log_summary(resp: Response, *, stored: bool, started: float) -> None:
    stats = current_stats()
    log.info(
        "message done type=%s reason=%s scheme=%s stage=%s validator=%s stored=%s "
        "llm_calls=%d tokens=%d prompt_tokens=%d completion_tokens=%d cached_tokens=%d "
        "latency_ms=%d",
        resp.type,
        resp.reason,
        resp.scheme,
        stats.stage if stats else None,
        stats.validator if stats else None,
        stored,
        stats.llm_calls if stats else 0,
        stats.total_tokens if stats else 0,
        stats.prompt_tokens if stats else 0,
        stats.completion_tokens if stats else 0,
        stats.cached_tokens if stats else 0,
        round(1000 * (time.monotonic() - started)),
    )
