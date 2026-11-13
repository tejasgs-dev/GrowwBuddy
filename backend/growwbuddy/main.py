"""FastAPI entry point (Phase 4).

    uvicorn growwbuddy.main:app --no-access-log      # what `make serve` runs

Run **one worker**: the per-thread locks live in this process (EC-THR-08).

Services (database, index, Groq client) are built at startup, not at import, so importing
this module has no side effects. Tests pass their own `Services` to `create_app`.

A background task deletes threads with no activity for `GROWWBUDDY_THREAD_RETENTION_DAYS`
(task 6.7): once at startup, then every few hours.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from growwbuddy.api.middleware import (
    REQUEST_ID_HEADER,
    BodySizeLimitMiddleware,
    RequestContextMiddleware,
)
from growwbuddy.api.routes import router, validation_error_handler
from growwbuddy.api.schemas import HealthOut
from growwbuddy.api.service import Services
from growwbuddy.config import Settings, settings
from growwbuddy.obs import configure_logging, install_record_factory

log = logging.getLogger(__name__)

RETENTION_SWEEP_S = 6 * 3600

# Request bodies are one short message (<= 1,000 characters) plus two UUIDs: a few hundred
# bytes, or ~4 KB of multi-byte text. 16 KB leaves room and stops a large body (EC-API-02).
MAX_BODY_BYTES = 16 * 1024


def build_services(s: Settings) -> Services:
    """The real services: SQLite store, vector index, embedder and Groq client."""
    from growwbuddy.api.limits import LimitConfig, MessageLimiter
    from growwbuddy.llm.client import GroqClient
    from growwbuddy.pipeline.orchestrator import Pipeline
    from growwbuddy.retrieval.embed import make_embedder
    from growwbuddy.retrieval.retriever import Retriever
    from growwbuddy.retrieval.vector import VectorIndex
    from growwbuddy.store.db import init_db, make_engine, make_session_factory
    from growwbuddy.store.threads import ThreadStore

    engine = make_engine(s.db_url)
    init_db(engine)
    sf = make_session_factory(engine)
    retriever = Retriever(
        VectorIndex(sf, s.embed_model),
        make_embedder(s),
        sf,
        min_score=s.retrieval_min_score,
        min_gap=s.retrieval_min_gap,
    )
    pipeline = Pipeline(
        GroqClient(s),
        retriever,
        history_turns=s.history_turns,
        fetched_at=retriever.fetched_at,
        stale_days=s.stale_days,
    )
    limiter = MessageLimiter(
        LimitConfig(s.client_per_minute, s.client_per_day, s.ip_per_minute, s.ip_per_day)
    )
    return Services(
        store=ThreadStore(sf),
        pipeline=pipeline,
        ready=retriever.ready,
        newest_date=retriever.newest_date,
        history_turns=s.history_turns,
        limiter=limiter,
    )


async def retention_loop(svc: Services, days: int, every_s: float = RETENTION_SWEEP_S) -> None:
    """Delete inactive threads now and then every `every_s` seconds (task 6.7)."""
    while True:
        try:
            deleted = await asyncio.to_thread(svc.store.delete_inactive, days)
            if deleted:
                log.info(
                    "retention: deleted %d inactive threads (older than %d days)", deleted, days
                )
        except Exception as e:  # keep the loop alive; try again next sweep
            log.error("retention sweep failed error=%s", type(e).__name__)
        await asyncio.sleep(every_s)


def create_app(
    services: Services | None = None,
    *,
    s: Settings = settings,
    configure_logs: bool = False,
) -> FastAPI:
    install_record_factory()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if configure_logs:
            configure_logging()
        if getattr(app.state, "services", None) is None:
            app.state.services = build_services(s)
        svc: Services = app.state.services
        if not await asyncio.to_thread(svc.ready):
            log.warning("index not ready: run `make ingest`; factual questions will fail")
        sweeper = (
            asyncio.create_task(retention_loop(svc, s.thread_retention_days))
            if s.thread_retention_days > 0
            else None
        )
        try:
            yield
        finally:
            if sweeper is not None:
                sweeper.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await sweeper

    app = FastAPI(title="GrowwBuddy", version="0.1.0", lifespan=lifespan)
    app.state.services = services

    app.include_router(router)
    app.add_exception_handler(RequestValidationError, validation_error_handler)

    @app.get("/healthz")
    async def healthz() -> HealthOut:
        """Liveness, plus whether the index is loaded (EC-RET-06). Always 200 while the
        process runs; `ready: false` means factual questions get "temporarily unavailable"."""
        svc: Services | None = app.state.services
        ready = svc is not None and await asyncio.to_thread(svc.ready)
        return HealthOut(status="ok", ready=ready)

    # The built web UI (task 5.10). Mounted last, so /api and /healthz always win.
    if (s.frontend_dist / "index.html").is_file():
        app.mount("/", StaticFiles(directory=s.frontend_dist, html=True), name="frontend")
    else:
        log.info("no built frontend at frontend_dist; API only (run `make frontend-build`)")

    # Added inner-to-outer: CORS sits outermost so even 413s carry CORS headers.
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=MAX_BODY_BYTES)
    app.add_middleware(RequestContextMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[s.frontend_origin],  # task 4.9, EC-API-04
        allow_methods=["GET", "POST", "DELETE"],
        allow_headers=["Content-Type", REQUEST_ID_HEADER],
        expose_headers=[REQUEST_ID_HEADER],
        allow_credentials=False,
    )
    return app


app = create_app(configure_logs=True)
