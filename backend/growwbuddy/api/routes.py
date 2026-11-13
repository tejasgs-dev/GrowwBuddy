"""HTTP endpoints (task 4.4, architecture §9.3).

    POST   /api/threads                     create a thread
    GET    /api/threads?client_id=          list the caller's threads
    GET    /api/threads/{id}/messages?client_id=   load a thread's history
    POST   /api/threads/{id}/messages       send a message, get the reply (§6.6)
    DELETE /api/threads/{id}?client_id=     delete a thread and its messages
    GET    /api/meta                        welcome text, examples, disclaimer, corpus date

A thread that doesn't exist and a thread owned by another `client_id` both give 404, so
thread IDs can't be probed (EC-THR-11, EC-API-07).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from growwbuddy.api.schemas import (
    CitationOut,
    ClientIdQuery,
    CreateThreadIn,
    MessageOut,
    MessagesOut,
    MetaOut,
    ReplyOut,
    SchemeOut,
    SendMessageIn,
    ThreadIdPath,
    ThreadListOut,
    ThreadOut,
)
from growwbuddy.api.service import Services, ThreadNotFoundError, send_message
from growwbuddy.constants import (
    DISCLAIMER,
    EXAMPLE_QUESTIONS,
    NEW_CHAT_TITLE,
    PII_INPUT_HINT,
    SCHEME_KEYS,
    SCHEME_NAMES,
    SOURCES,
    format_footer,
)
from growwbuddy.obs import set_thread_id
from growwbuddy.pipeline import refusals
from growwbuddy.pipeline.types import Citation
from growwbuddy.store.threads import StoredMessage, ThreadInfo

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api")

_NOT_FOUND = "thread not found"


def get_services(request: Request) -> Services:
    svc: Services = request.app.state.services
    return svc


ServicesDep = Annotated[Services, Depends(get_services)]


def _thread_out(t: ThreadInfo) -> ThreadOut:
    return ThreadOut(
        thread_id=t.id,
        title=t.title or NEW_CHAT_TITLE,
        created_at=t.created_at,
        updated_at=t.updated_at,
    )


def _citation(c: Citation | None) -> CitationOut | None:
    return CitationOut(url=c.url, title=c.title) if c else None


def _message_out(m: StoredMessage) -> MessageOut:
    return MessageOut(
        message_id=m.id,
        role=m.role,
        text=m.content,
        type=m.response_type,
        citation=(
            CitationOut(url=m.citation_url, title=m.citation_title or "")
            if m.citation_url
            else None
        ),
        last_updated=m.as_of_date,
        footer=m.footer,
        created_at=m.created_at,
    )


def _not_found() -> HTTPException:
    return HTTPException(status.HTTP_404_NOT_FOUND, _NOT_FOUND)


# ---------------------------------------------------------------- threads


@router.post("/threads", status_code=status.HTTP_201_CREATED)
async def create_thread(body: CreateThreadIn, svc: ServicesDep) -> ThreadOut:
    t = await asyncio.to_thread(svc.store.create, str(body.client_id))
    set_thread_id(t.id)
    log.info("thread created")
    return _thread_out(t)


@router.get("/threads")
async def list_threads(client_id: ClientIdQuery, svc: ServicesDep) -> ThreadListOut:
    threads = await asyncio.to_thread(svc.store.list_for, str(client_id))
    return ThreadListOut(threads=[_thread_out(t) for t in threads])


@router.get("/threads/{thread_id}/messages")
async def get_messages(
    thread_id: ThreadIdPath,
    client_id: ClientIdQuery,
    svc: ServicesDep,
) -> MessagesOut:
    set_thread_id(thread_id)
    t = await asyncio.to_thread(svc.store.get, thread_id, str(client_id))
    if t is None:
        raise _not_found()
    messages = await asyncio.to_thread(svc.store.messages, thread_id)
    return MessagesOut(thread=_thread_out(t), messages=[_message_out(m) for m in messages])


@router.post("/threads/{thread_id}/messages")
async def post_message(
    thread_id: ThreadIdPath,
    body: SendMessageIn,
    svc: ServicesDep,
    request: Request,
) -> ReplyOut:
    try:
        result = await send_message(
            svc,
            thread_id,
            str(body.client_id),
            body.text,
            str(body.client_message_id) if body.client_message_id else None,
            # The direct peer. Behind a reverse proxy this is the proxy's address; run
            # uvicorn with --proxy-headers (and --forwarded-allow-ips) so it's the client's.
            client_ip=request.client.host if request.client else None,
        )
    except ThreadNotFoundError:
        raise _not_found() from None
    r = result.response
    return ReplyOut(
        message_id=result.message_id,
        thread_id=result.thread.id,
        type=r.type,
        text=r.text,
        citation=_citation(r.citation),
        last_updated=r.last_updated,
        footer=r.footer,
        thread_title=result.thread.title or NEW_CHAT_TITLE,
    )


@router.delete("/threads/{thread_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_thread(
    thread_id: ThreadIdPath,
    client_id: ClientIdQuery,
    svc: ServicesDep,
) -> None:
    set_thread_id(thread_id)
    if not await asyncio.to_thread(svc.store.delete, thread_id, str(client_id)):
        raise _not_found()
    log.info("thread deleted")


# ---------------------------------------------------------------- meta


@router.get("/meta")
async def meta(svc: ServicesDep) -> MetaOut:
    newest = await asyncio.to_thread(svc.newest_date)
    urls = {scheme: url for _, scheme, url in SOURCES}
    return MetaOut(
        welcome=refusals.WELCOME,
        example_questions=list(EXAMPLE_QUESTIONS),
        disclaimer=DISCLAIMER,
        input_hint=PII_INPUT_HINT,
        last_updated=newest,
        footer=format_footer(newest) if newest else None,
        schemes=[SchemeOut(key=k, name=SCHEME_NAMES[k], url=urls[k]) for k in SCHEME_KEYS],
    )


# ---------------------------------------------------------------- errors


async def validation_error_handler(_request: Request, exc: Exception) -> JSONResponse:
    """422 with where and what kind of error only (EC-API-01, EV-4-02).

    FastAPI's default body includes the rejected `input` (and pydantic's message can quote
    part of it). Both are left out, so a PAN typed into the wrong field is never echoed.
    """
    assert isinstance(exc, RequestValidationError)
    errors = [
        {"loc": [str(p) for p in err.get("loc", ())], "type": err.get("type", "invalid")}
        for err in exc.errors()
    ]
    log.info("request rejected: validation errors=%s", [e["type"] for e in errors])
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        content=jsonable_encoder({"detail": "invalid request", "errors": errors}),
    )
