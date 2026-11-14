"""The answer pipeline end to end (task 3.12, architecture §6).

    PII guard -> rules -> LLM router -> retriever -> generator -> validator -> formatter

`handle_message(text, history)` always returns a `Response`; it never raises for model,
network or index problems. Rule-handled messages (PII, advisory, performance/NAV,
greeting, out of scope) make no model call, so they keep working when Groq is down or
the budget is used up (EV-3-21).

Every reply whose cited source hasn't been fetched in `stale_days` gets "(source may be
out of date)" added to its footer, outside the 3-sentence body, and logs an alert
(task 6.6, EC-RET-08, EC-VAL-09).

Logs carry types, intents and counts only, never the message text (§11).
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Protocol

from growwbuddy.constants import STALE_NOTE
from growwbuddy.llm.client import StructuredLLM
from growwbuddy.obs import note, timed
from growwbuddy.pipeline import refusals
from growwbuddy.pipeline.formatter import answer_response, template_response
from growwbuddy.pipeline.generator import Generation, generate
from growwbuddy.pipeline.pii_guard import check as pii_check
from growwbuddy.pipeline.router import classify
from growwbuddy.pipeline.router_rules import RuleDecision, decide
from growwbuddy.pipeline.types import Response, ResponseType, Turn
from growwbuddy.pipeline.validator import check_response, validate_answer
from growwbuddy.retrieval.embed import EmbedderUnavailableError
from growwbuddy.retrieval.headers import RetrievalHeaders, mentioned_schemes
from growwbuddy.retrieval.retriever import PageInfo, Retrieval
from growwbuddy.retrieval.vector import IndexNotReadyError, MixedIndexError

log = logging.getLogger(__name__)

MAX_MESSAGE_CHARS = 1000  # EC-INT-27
MAX_ANSWER_ATTEMPTS = 2  # first try + one retry with feedback (EC-GEN-14)

# Replies that aren't written to the thread: PII is never stored (P6), and a busy reply
# isn't a failed answer, so the user can simply send the message again (task 3.4).
NOT_STORED: frozenset[str] = frozenset({"pii_rejected", "busy"})


def should_store(resp: Response) -> bool:
    return resp.type not in NOT_STORED


class RetrieverLike(Protocol):
    def retrieve(self, query: str, scheme: str) -> Retrieval: ...

    def page(self, scheme: str) -> PageInfo | None: ...

    def newest_date(self) -> date | None: ...


def needs_model(text: str, headers: RetrievalHeaders | None = None) -> bool:
    """Whether this message would reach the LLM router: not empty, not too long, no PII,
    and not decided by the stage 1 rules. Rule-handled messages cost no Groq tokens, so
    the per-client limits (task 6.4) don't count them. History never changes this."""
    if not text.strip() or len(text) > MAX_MESSAGE_CHARS:
        return False
    if pii_check(text).found:
        return False
    return decide(text, headers) is None


FetchedAt = Callable[[str], datetime | None]  # source URL -> last successful fetch


def history_scheme(history: Sequence[Turn]) -> str | None:
    """The most recent scheme in the thread (task 1.2, EC-THR-02, 04)."""
    return next((t.scheme for t in reversed(history) if t.scheme), None)


class Pipeline:
    def __init__(
        self,
        llm: StructuredLLM,
        retriever: RetrieverLike,
        *,
        headers: RetrievalHeaders | None = None,
        history_turns: int = 6,
        fetched_at: FetchedAt | None = None,
        stale_days: int | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.llm = llm
        self.retriever = retriever
        self.headers = headers
        self.history_turns = history_turns
        self._fetched_at = fetched_at
        self.stale_days = stale_days
        self._clock = clock

    # ------------------------------------------------------------ helpers

    def _reply(
        self,
        type_: ResponseType,
        text: str,
        *,
        scheme: str | None = None,
        reason: str | None = None,
    ) -> Response:
        return template_response(type_, text, scheme=scheme, reason=reason, pages=self.retriever)

    def _refusal(self, reason: str, scheme: str | None) -> Response:
        texts = {
            "advisory": refusals.advisory,
            "comparison": refusals.comparison,
        }
        return self._reply("refusal", texts[reason](scheme), scheme=scheme, reason=reason)

    def _performance(self, scheme: str | None) -> Response:
        return self._reply(
            "performance", refusals.performance(scheme), scheme=scheme, reason="performance"
        )

    def _not_found(self, scheme: str | None, reason: str = "not_found") -> Response:
        return self._reply("not_found", refusals.not_found(scheme), scheme=scheme, reason=reason)

    def _fallback(self, scheme: str | None) -> Response:
        return self._reply("not_found", refusals.fallback(scheme), scheme=scheme, reason="fallback")

    def _busy(self) -> Response:
        return Response(type="busy", text=refusals.BUSY, reason="rate_limited")

    def _unavailable(self, reason: str) -> Response:
        return Response(type="unavailable", text=refusals.UNAVAILABLE, reason=reason)

    def _mark_if_stale(self, resp: Response) -> Response:
        """Add the staleness note to the footer when the cited source is old (task 6.6)."""
        if (
            self._fetched_at is None
            or self.stale_days is None
            or resp.citation is None
            or not resp.footer
        ):
            return resp
        try:
            fetched = self._fetched_at(resp.citation.url)
        except Exception as e:  # a lookup problem must never block the reply
            log.warning("staleness check failed error=%s", type(e).__name__)
            return resp
        if fetched is None:
            return resp
        age = self._clock() - fetched
        if age <= timedelta(days=self.stale_days):
            return resp
        log.warning(
            "stale source alert: scheme=%s last_fetched=%s age_days=%d stale_days=%d",
            resp.scheme,
            fetched.date().isoformat(),
            age.days,
            self.stale_days,
        )
        return dataclasses.replace(resp, footer=f"{resp.footer} {STALE_NOTE}")

    # ------------------------------------------------------------ entry point

    async def handle_message(self, text: str, history: Sequence[Turn] = ()) -> Response:
        resp, stage = await self._handle(text, history)
        with timed("final_check"):
            problems = check_response(resp)
        if problems:
            # Should never happen: the templates and formatter are built to pass.
            log.error("response failed final check type=%s problems=%s", resp.type, problems)
            resp = self._fallback(resp.scheme)
        with timed("stale_check"):  # one sources_state read
            resp = self._mark_if_stale(resp)
        note(stage=stage)
        log.info(
            "message handled stage=%s type=%s reason=%s scheme=%s",
            stage,
            resp.type,
            resp.reason,
            resp.scheme,
        )
        return resp

    async def _handle(self, text: str, history: Sequence[Turn]) -> tuple[Response, str]:
        if not text.strip():
            return self._reply("greeting", refusals.WELCOME, reason="empty"), "input"
        if len(text) > MAX_MESSAGE_CHARS:
            return self._reply("clarification", refusals.TOO_LONG, reason="too_long"), "input"

        # 1. PII guard: before anything is stored, logged or sent to the model.
        pii = pii_check(text)
        if pii.found:
            log.info("pii rejected types=%s", ",".join(pii.types))
            return Response(type="pii_rejected", text=refusals.PII, reason="pii"), "pii"

        # 2. Stage 1 rules: no model call.
        with timed("rules"):
            prior = history_scheme(history)
            rule = decide(text, self.headers)
        if rule is not None:
            return self._from_rule(rule, prior), f"rules:{rule.kind}"

        # 3. Stage 2 LLM router.
        with timed("router"):
            outcome = await classify(text, history, self.llm, self.history_turns)
        if outcome.decision is None:
            if outcome.result.rate_limited:
                return self._busy(), "router"
            if outcome.result.error and outcome.result.error.startswith("api error"):
                return self._unavailable("llm_down"), "router"
            return self._fallback(prior), "router"
        decision = outcome.decision
        intent = decision.final_intent

        # A scheme the message names outright beats the model's reading of it.
        named = mentioned_schemes(text, self.headers)
        scheme = named[0] if len(named) == 1 else decision.scheme

        if intent == "greeting":
            return self._reply("greeting", refusals.WELCOME, reason="greeting"), "router"
        if intent == "advisory":
            return self._refusal("advisory", scheme or prior), "router"
        if intent == "performance":
            return self._performance(scheme or prior), "router"
        if intent == "out_of_scope":
            return self._reply("refusal", refusals.out_of_scope(), reason="out_of_scope"), "router"

        # factual / procedural
        if scheme is None:
            # EC-RET-03, EC-THR-01: never search across schemes; ask which one.
            return self._reply(
                "clarification", refusals.which_scheme(), reason="no_scheme"
            ), "router"
        return await self._answer(decision.rewritten_query or text, scheme), "answer"

    def _from_rule(self, rule: RuleDecision, prior: str | None) -> Response:
        scheme = rule.scheme or prior
        match rule.kind:
            case "greeting":
                return self._reply("greeting", refusals.WELCOME, reason="greeting")
            case "thanks":
                return self._reply("greeting", refusals.THANKS, reason="thanks")
            case "advisory" | "comparison":
                return self._refusal(rule.kind, scheme)
            case "performance":
                return self._performance(scheme)
            case "elss":
                return self._reply("refusal", refusals.elss(), reason="elss")
            case "out_of_scope":
                return self._reply("refusal", refusals.out_of_scope(), reason="out_of_scope")
            case "personal":
                return self._reply("refusal", refusals.personal(), reason="personal")
            case "internals":
                return self._reply("refusal", refusals.internals(), reason="internals")
            case "plan_not_covered":
                return self._reply(
                    "not_found",
                    refusals.plan_not_covered(scheme),
                    scheme=scheme,
                    reason="plan_not_covered",
                )
            case "not_in_corpus":
                return self._not_found(scheme, reason="not_in_corpus")
            case "multi_scheme":
                # EC-INT-18, 19: one citation per answer, so one scheme at a time.
                text = (
                    refusals.one_scheme_at_a_time(rule.schemes)
                    if len(rule.schemes) >= 2
                    else refusals.which_scheme()
                )
                return self._reply("clarification", text, reason="multi_scheme")
        raise AssertionError(f"unhandled rule {rule.kind}")  # pragma: no cover

    # ------------------------------------------------------------ factual path

    async def _answer(self, query: str, scheme: str) -> Response:
        try:
            with timed("retrieval"):  # includes the `embed` step
                retrieval = await asyncio.to_thread(self.retriever.retrieve, query, scheme)
        except (IndexNotReadyError, MixedIndexError, EmbedderUnavailableError) as e:
            log.error("retrieval unavailable: %s", type(e).__name__)
            return self._unavailable("index_unavailable")
        if retrieval.low_confidence or not retrieval.items:
            return self._not_found(scheme, reason="low_confidence")

        feedback: tuple[str, ...] = ()
        for attempt in range(MAX_ANSWER_ATTEMPTS):
            with timed("generation"):
                gen: Generation = await generate(
                    query, retrieval.items, self.llm, feedback=feedback
                )
            if gen.failure == "busy":
                return self._busy()
            if gen.failure == "unavailable":
                return self._unavailable("llm_down")
            if gen.failure == "bad_output":
                log.warning("answer rejected: %s", gen.error)
                return self._fallback(scheme)
            if gen.failure == "schema" or gen.answer is None:
                log.warning("answer failed schema on attempt %d: %s", attempt + 1, gen.error)
                feedback = (gen.error or "the answer didn't match the schema",)
                continue

            answer = gen.answer
            if not answer.is_answer:  # EC-GEN-08, 09
                note(validator="not_answerable")
                return self._not_found(scheme)
            with timed("validation"):
                check = validate_answer(answer, retrieval, scheme, self.headers)
            if check.action == "advice":
                note(validator="advice")
                return self._refusal("advisory", scheme)
            if check.action == "performance":
                note(validator="performance")
                return self._performance(scheme)
            if check.ok and answer.source_chunk_id:
                cited = retrieval.get(answer.source_chunk_id)
                assert cited is not None  # validate_answer checked it
                note(validator="pass" if attempt == 0 else "retry_pass")
                return answer_response(answer.sentences, cited)
            log.warning("answer failed validation on attempt %d: %s", attempt + 1, check.errors)
            feedback = check.errors
        note(validator="fail")
        return self._fallback(scheme)
