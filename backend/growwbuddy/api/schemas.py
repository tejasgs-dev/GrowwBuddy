"""Request and response models for the HTTP API (task 4.4, architecture §6.6, §9.3)."""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated
from uuid import UUID

from fastapi import Path, Query
from pydantic import BaseModel, Field, field_validator

from growwbuddy.ids import THREAD_ID_PATTERN
from growwbuddy.pipeline.orchestrator import MAX_MESSAGE_CHARS
from growwbuddy.pipeline.types import ResponseType

# `client_id` is a random UUID from the browser (task 5.2). Anything else is a 422, so
# path traversal or SQL text never reaches the store (EC-API-03).
ClientIdQuery = Annotated[UUID, Query(description="Anonymous browser ID (random UUID)")]
ThreadIdPath = Annotated[str, Path(pattern=THREAD_ID_PATTERN, max_length=32)]


class CreateThreadIn(BaseModel):
    client_id: UUID


class SendMessageIn(BaseModel):
    client_id: UUID
    text: str = Field(max_length=MAX_MESSAGE_CHARS)
    # Optional ID the UI picks per send; a repeat with the same ID gets the stored reply
    # instead of a second answer (EC-THR-07).
    client_message_id: UUID | None = None

    @field_validator("text")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("text is empty")
        return v


class ThreadOut(BaseModel):
    thread_id: str
    title: str
    created_at: datetime
    updated_at: datetime


class ThreadListOut(BaseModel):
    threads: list[ThreadOut]


class CitationOut(BaseModel):
    url: str
    title: str


class ReplyOut(BaseModel):
    """The reply to one sent message (architecture §6.6).

    `message_id` is None for replies that aren't stored: `pii_rejected` and `busy`.
    """

    message_id: str | None
    thread_id: str
    type: ResponseType
    text: str
    citation: CitationOut | None
    last_updated: date | None
    footer: str | None
    thread_title: str


class MessageOut(BaseModel):
    message_id: str
    role: str  # user | assistant
    text: str
    type: str | None  # assistant rows only
    citation: CitationOut | None
    last_updated: date | None
    footer: str | None
    created_at: datetime


class MessagesOut(BaseModel):
    thread: ThreadOut
    messages: list[MessageOut]


class SchemeOut(BaseModel):
    key: str
    name: str
    url: str


class MetaOut(BaseModel):
    welcome: str
    example_questions: list[str]
    disclaimer: str
    input_hint: str
    last_updated: date | None  # newest data date across the corpus
    footer: str | None
    schemes: list[SchemeOut]


class HealthOut(BaseModel):
    status: str
    ready: bool  # False until an index is built (EC-RET-06)
