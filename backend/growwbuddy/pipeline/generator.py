"""Answer generator (task 3.9, architecture §6.4 / §7).

Builds the context with each item labelled by its ID, calls Groq with the strict answer
schema, and parses the result. The model cites by ID only; code looks up the URL later
(§7.2), so the model can never invent a link.

Failures are sorted into what the orchestrator should do:
- `busy`: rate limited (task 3.4) -> the busy reply, not "not found";
- `unavailable`: API down, timeout, 5xx or missing key (EC-GEN-01, 03);
- `bad_output`: finish_reason other than "stop", empty message, invalid JSON or a refusal
  (EC-GEN-04, 05) -> safe fallback, no retry;
- `schema`: valid JSON that breaks the answer model (e.g. 4 sentences) -> the validator's
  one retry applies (EC-GEN-06).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from pydantic import ValidationError

from growwbuddy.llm.client import JsonResult, StructuredLLM
from growwbuddy.llm.prompts import load_prompt
from growwbuddy.llm.schemas import ANSWER_SCHEMA, ANSWER_SCHEMA_NAME, Answer
from growwbuddy.retrieval.retriever import ContextItem

ANSWER_MAX_TOKENS = 1024

Failure = Literal["busy", "unavailable", "bad_output", "schema"]


@dataclass(frozen=True)
class Generation:
    answer: Answer | None
    result: JsonResult
    failure: Failure | None = None
    error: str | None = None


def classify_failure(result: JsonResult) -> Failure:
    if result.rate_limited:
        return "busy"
    if result.error and result.error.startswith("api error"):
        return "unavailable"
    return "bad_output"


def render_context(items: Sequence[ContextItem]) -> str:
    lines = []
    for item in items:
        marker = "Verified fact. " if item.kind == "fact" else ""
        lines.append(f"[{item.item_id}] {marker}{item.text}")
    return "\n".join(lines)


def build_user_message(
    question: str, items: Sequence[ContextItem], feedback: Sequence[str] = ()
) -> str:
    msg = f"Context:\n{render_context(items)}\n\nQuestion: {question}"
    if feedback:
        problems = "; ".join(feedback)
        msg += (
            f"\n\nYour previous answer was rejected: {problems}. "
            "Answer again, following every rule."
        )
    return msg


async def generate(
    question: str,
    items: Sequence[ContextItem],
    llm: StructuredLLM,
    *,
    feedback: Sequence[str] = (),
) -> Generation:
    result = await llm.complete_json(
        system=load_prompt("answer_system"),
        user=build_user_message(question, items, feedback),
        schema_name=ANSWER_SCHEMA_NAME,
        schema=ANSWER_SCHEMA,
        max_tokens=ANSWER_MAX_TOKENS,
        kind="answer_retry" if feedback else "answer",
    )
    if not result.ok or result.data is None:
        return Generation(None, result, classify_failure(result), result.error)
    try:
        return Generation(Answer.model_validate(result.data), result)
    except ValidationError as e:
        # Only the error types go back to the model, never input values.
        reasons = sorted({err["msg"] for err in e.errors()})
        return Generation(None, result, "schema", "; ".join(reasons))
