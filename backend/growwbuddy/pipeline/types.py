"""Response and thread-history types shared by the pipeline (architecture §6.6)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Literal

ResponseType = Literal[
    "answer",
    "refusal",
    "performance",
    "not_found",
    "pii_rejected",
    "greeting",
    "clarification",
    "busy",
    "unavailable",
]

# Replies that never carry a link or a footer (EC-VAL-08, plan task 3.2).
NO_LINK_TYPES: frozenset[str] = frozenset(
    {"pii_rejected", "greeting", "clarification", "busy", "unavailable"}
)


@dataclass(frozen=True)
class Citation:
    url: str
    title: str


@dataclass(frozen=True)
class Response:
    type: ResponseType
    text: str
    citation: Citation | None = None
    last_updated: date | None = None
    footer: str | None = None
    # Why a non-answer was given, e.g. "advisory", "comparison", "out_of_scope", "elss".
    reason: str | None = None
    # The scheme this reply is about; carried into the thread so follow-ups resolve.
    scheme: str | None = None


@dataclass(frozen=True)
class Turn:
    """One stored exchange. PII-rejected messages are never stored (EC-PII-20)."""

    user_text: str
    response_type: str
    scheme: str | None
    response_text: str = ""
