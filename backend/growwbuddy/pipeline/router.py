"""Stage 2 of the router: the LLM classifier (task 3.7, architecture §6.2).

Returns `{intent, scheme, rewritten_query}` plus `may_be_advice`, which applies the
tie-break rule: if the message could be asking for advice, the final intent is `advisory`.

The thread history goes in the user message, never the system prompt, so the system
prompt stays cacheable.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from pydantic import ValidationError

from growwbuddy.llm.client import JsonResult, StructuredLLM
from growwbuddy.llm.prompts import load_prompt
from growwbuddy.llm.schemas import ROUTER_SCHEMA, ROUTER_SCHEMA_NAME, RouterDecision
from growwbuddy.pipeline.types import Turn

ROUTER_MAX_TOKENS = 512


@dataclass(frozen=True)
class RouterOutcome:
    decision: RouterDecision | None
    result: JsonResult  # for the failure reason (rate limited vs other)


def render_history(history: Sequence[Turn], turns: int) -> str:
    recent = list(history)[-turns:] if turns > 0 else []
    if not recent:
        return "(no earlier turns)"
    lines = []
    for t in recent:
        about = t.scheme or "none"
        lines.append(f"- user: {t.user_text}\n  reply: {t.response_type} (scheme: {about})")
    return "\n".join(lines)


async def classify(
    text: str, history: Sequence[Turn], llm: StructuredLLM, history_turns: int
) -> RouterOutcome:
    user = (
        f"Conversation so far:\n{render_history(history, history_turns)}\n\nLatest message:\n{text}"
    )
    result = await llm.complete_json(
        system=load_prompt("router_system"),
        user=user,
        schema_name=ROUTER_SCHEMA_NAME,
        schema=ROUTER_SCHEMA,
        max_tokens=ROUTER_MAX_TOKENS,
        kind="router",
    )
    if not result.ok or result.data is None:
        return RouterOutcome(None, result)
    try:
        return RouterOutcome(RouterDecision.model_validate(result.data), result)
    except ValidationError:
        return RouterOutcome(None, JsonResult(False, error="router output failed validation"))
