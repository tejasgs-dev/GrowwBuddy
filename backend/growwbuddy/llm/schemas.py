"""Router and answer schemas (task 3.5, architecture §7.3 as adjusted for Groq).

What's sent to Groq avoids features its strict mode isn't documented to support
(implementation plan, "Structured outputs: docs check"):
- no `minItems`/`maxItems`: the 1-3 sentence limit is enforced here and by the validator;
- no nullable unions: "no scheme" is the string `"none"`, "no citation" is `""`.

The Pydantic models turn those sentinels back into `None`.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, field_validator

from growwbuddy.constants import SCHEME_KEYS

Intent = Literal["factual", "procedural", "performance", "advisory", "out_of_scope", "greeting"]
INTENTS: tuple[str, ...] = (
    "factual",
    "procedural",
    "performance",
    "advisory",
    "out_of_scope",
    "greeting",
)
NO_SCHEME = "none"
MAX_SENTENCES = 3

ROUTER_SCHEMA_NAME = "router_decision"
ROUTER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "intent": {"type": "string", "enum": list(INTENTS)},
        "may_be_advice": {"type": "boolean"},
        "scheme": {"type": "string", "enum": [*SCHEME_KEYS, NO_SCHEME]},
        "rewritten_query": {"type": "string"},
    },
    "required": ["intent", "may_be_advice", "scheme", "rewritten_query"],
    "additionalProperties": False,
}

ANSWER_SCHEMA_NAME = "grounded_answer"
ANSWER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "answerable": {"type": "boolean"},
        "sentences": {"type": "array", "items": {"type": "string"}},
        "source_chunk_id": {"type": "string"},
    },
    "required": ["answerable", "sentences", "source_chunk_id"],
    "additionalProperties": False,
}


class RouterDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    intent: Intent
    # True when the message could be asking for advice, a recommendation, a prediction or a
    # judgement. The tie-break rule (§6.2) turns that into `advisory`.
    may_be_advice: bool
    scheme: str | None
    rewritten_query: str

    @field_validator("scheme", mode="before")
    @classmethod
    def _scheme(cls, v: object) -> object:
        if v in (None, "", NO_SCHEME):
            return None
        if v not in SCHEME_KEYS:
            raise ValueError(f"unknown scheme {v!r}")
        return v

    @property
    def final_intent(self) -> str:
        """Tie-break: if unsure between factual and advisory, choose advisory (EC-INT-01)."""
        if self.may_be_advice and self.intent in ("factual", "procedural"):
            return "advisory"
        return self.intent


class Answer(BaseModel):
    """Model output for the answer call. Empty or over-long answers fail validation here."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    answerable: bool
    sentences: tuple[str, ...]
    source_chunk_id: str | None

    @field_validator("source_chunk_id", mode="before")
    @classmethod
    def _chunk_id(cls, v: object) -> object:
        if isinstance(v, str) and not v.strip():
            return None
        return v

    @field_validator("sentences", mode="before")
    @classmethod
    def _sentences(cls, v: object) -> object:
        if isinstance(v, list):
            items = tuple(s.strip() for s in v if isinstance(s, str) and s.strip())
            if len(items) > MAX_SENTENCES:
                raise ValueError(f"{len(items)} sentences; at most {MAX_SENTENCES} allowed")
            return items
        return v

    @property
    def is_answer(self) -> bool:
        """`answerable: true` with no real sentence is treated as not answerable (EC-GEN-08)."""
        return self.answerable and bool(self.sentences)
