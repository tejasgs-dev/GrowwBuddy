"""Request-scoped log context and structured logging (task 4.8, architecture §11).

Every log record carries `request_id` and `thread_id` from context variables, so lines
written deep in the pipeline (router, retriever, Groq client) can be tied to one request
without passing IDs around. `RequestStats` collects what the per-message summary line
reports: Groq calls and tokens, the pipeline stage and the validator result.

Nothing here ever logs message text. Callers log types, intents, counts and IDs only.
"""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any

_request_id: ContextVar[str | None] = ContextVar("growwbuddy_request_id", default=None)
_thread_id: ContextVar[str | None] = ContextVar("growwbuddy_thread_id", default=None)
_stats: ContextVar[RequestStats | None] = ContextVar("growwbuddy_request_stats", default=None)


@dataclass
class RequestStats:
    llm_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    total_tokens: int = 0
    stage: str | None = None
    # pass | retry_pass | fail | advice | performance | not_answerable; None = no model answer
    validator: str | None = None

    def add_llm_call(self, usage: Mapping[str, int]) -> None:
        self.llm_calls += 1
        self.prompt_tokens += int(usage.get("prompt_tokens", 0))
        self.completion_tokens += int(usage.get("completion_tokens", 0))
        self.cached_tokens += int(usage.get("cached_tokens", 0))
        self.total_tokens += int(usage.get("total_tokens", 0))

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@contextmanager
def request_context(request_id: str) -> Iterator[RequestStats]:
    """Bind a request ID (and fresh stats) for the duration of one request."""
    stats = RequestStats()
    tokens = (_request_id.set(request_id), _thread_id.set(None), _stats.set(stats))
    try:
        yield stats
    finally:
        _stats.reset(tokens[2])
        _thread_id.reset(tokens[1])
        _request_id.reset(tokens[0])


def set_thread_id(thread_id: str | None) -> None:
    _thread_id.set(thread_id)


def current_request_id() -> str | None:
    return _request_id.get()


def current_stats() -> RequestStats | None:
    return _stats.get()


def note(*, stage: str | None = None, validator: str | None = None) -> None:
    """Record the pipeline stage or validator result on the current request, if any."""
    stats = _stats.get()
    if stats is None:
        return
    if stage is not None:
        stats.stage = stage
    if validator is not None:
        stats.validator = validator


def record_llm_call(usage: Mapping[str, int]) -> None:
    stats = _stats.get()
    if stats is not None:
        stats.add_llm_call(usage)


# ---------------------------------------------------------------- logging setup

_factory_installed = False


def install_record_factory() -> None:
    """Make every LogRecord carry `request_id` and `thread_id` (idempotent).

    A record factory, rather than a handler filter, so the fields are present whichever
    handler receives the record (uvicorn's, pytest's caplog, ours).
    """
    global _factory_installed
    if _factory_installed:
        return
    previous = logging.getLogRecordFactory()

    def factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
        record = previous(*args, **kwargs)
        record.request_id = _request_id.get()
        record.thread_id = _thread_id.get()
        return record

    logging.setLogRecordFactory(factory)
    _factory_installed = True


class JsonFormatter(logging.Formatter):
    """One JSON object per line. Exceptions are reduced to their type: a traceback's
    message could quote user input, so it is never written (§11)."""

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "request_id": getattr(record, "request_id", None),
            "thread_id": getattr(record, "thread_id", None),
            "msg": record.getMessage(),
        }
        if record.exc_info and record.exc_info[0] is not None:
            entry["exc_type"] = record.exc_info[0].__name__
        return json.dumps(entry, ensure_ascii=False, default=str)


def configure_logging(level: str = "INFO") -> None:
    """JSON logs to stderr for the server. Replaces uvicorn's own handlers so its error
    lines use the same format; uvicorn's access log is off (EC-API-09): the request
    middleware writes a safer one."""
    install_record_factory()
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    for name in ("uvicorn", "uvicorn.error"):
        lg = logging.getLogger(name)
        lg.handlers.clear()
        lg.propagate = True
    access = logging.getLogger("uvicorn.access")
    access.handlers.clear()
    access.propagate = False
    access.disabled = True
    # httpx logs every outgoing URL at INFO; keep it quiet.
    logging.getLogger("httpx").setLevel(logging.WARNING)
