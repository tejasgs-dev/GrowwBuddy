"""Live-model eval runner (task 6.2, eval.md §8.5).

Runs each golden item through the real send-message flow (PII guard, rules, router,
retriever, generator, validator, formatter and the thread store) against the live Groq
model, grades the last turn, and writes a report.

Staying inside Groq's limits (implementation plan, "Task 6.2 notes"):

- **Rule-handled items first.** Items whose every turn is decided without the model (PII,
  rules) cost nothing and run first, unthrottled.
- **Throttled, one at a time.** Before each model item the runner waits until the last
  minute's tokens and requests plus this item's estimate fit in `share` (80%) of the
  per-minute limits. The estimate is the measured average tokens per model turn.
- **Daily reserve.** It stops cleanly before the day's tokens would leave less than
  `reserve` (20,000) for the app and ingest.
- **Checkpoint and resume.** Each result is appended to `progress.jsonl` as soon as it's
  graded; `--resume` skips finished items. A partial report is labelled as partial.
- **429s.** A rate-limited item is retried after Groq's `retry-after` when that's short
  (`max_wait`); otherwise the run checkpoints and stops.

Eval threads live in a separate database (`data/eval.db`), never the app's. The index and
facts are read from the app's database. Never run this in CI (EC-EVAL-04).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import subprocess
import time
from collections import deque
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from growwbuddy.api.service import Services, send_message
from growwbuddy.config import PROJECT_ROOT, Settings
from growwbuddy.constants import SOURCES
from growwbuddy.eval.golden import GoldenItem
from growwbuddy.eval.graders import Context, Observed, grade
from growwbuddy.llm.client import JsonResult, StructuredLLM
from growwbuddy.obs import JsonFormatter, install_record_factory, request_context
from growwbuddy.pipeline.formatter import footer_date
from growwbuddy.pipeline.orchestrator import Pipeline, RetrieverLike, needs_model
from growwbuddy.retrieval.retriever import PageInfo, Retrieval, Retriever
from growwbuddy.store.models import ChunkRow, SchemeFact
from growwbuddy.store.threads import ThreadStore

log = logging.getLogger(__name__)

EVAL_CLIENT_ID = "e7a1c0de-0000-4000-8000-000000000001"
DEFAULT_TOKENS_PER_MODEL_TURN = 2500  # implementation plan, "Rough budget"
PROMPTS_DIR = Path(__file__).resolve().parents[2] / "prompts"
NO_LLM_ERROR = "api error: eval run with --no-llm"
USAGE_KEYS = ("prompt_tokens", "completion_tokens", "cached_tokens", "total_tokens")

# fact_key -> the chunk section holding it, for retrieval recall@5.
FACT_SECTIONS: dict[str, str] = {
    "expense_ratio_base": "expense_ratio",
    "expense_ratio_direct": "expense_ratio",
    "exit_load": "exit_load",
    "min_sip": "minimum_investment",
    "min_lumpsum": "minimum_investment",
    "benchmark": "benchmark",
    "riskometer": "riskometer",
    "fund_manager": "fund_manager",
}
_SOURCE_OF: dict[str, str] = {scheme: sid for sid, scheme, _ in SOURCES}


# ---------------------------------------------------------------- spies


class SpyLLM:
    """Counts model calls and remembers the last result; can refuse to call (--no-llm)."""

    def __init__(self, inner: StructuredLLM | None) -> None:
        self.inner = inner
        self.calls = 0
        self.last: JsonResult | None = None
        self.usage: dict[str, int] = dict.fromkeys(USAGE_KEYS, 0)

    async def complete_json(
        self,
        *,
        system: str,
        user: str,
        schema_name: str,
        schema: dict[str, Any],
        max_tokens: int,
        kind: str = "call",
    ) -> JsonResult:
        self.calls += 1
        if self.inner is None:
            self.last = JsonResult(False, error=NO_LLM_ERROR)
            return self.last
        self.last = await self.inner.complete_json(
            system=system,
            user=user,
            schema_name=schema_name,
            schema=schema,
            max_tokens=max_tokens,
            kind=kind,
        )
        for k in USAGE_KEYS:
            self.usage[k] += int(self.last.usage.get(k, 0))
        return self.last


class SpyRetriever:
    """Delegates to the real retriever and remembers the last retrieval."""

    def __init__(self, inner: Retriever) -> None:
        self.inner = inner
        self.last: Retrieval | None = None

    def retrieve(self, query: str, scheme: str) -> Retrieval:
        self.last = self.inner.retrieve(query, scheme)
        return self.last

    def page(self, scheme: str) -> PageInfo | None:
        return self.inner.page(scheme)

    def newest_date(self) -> date | None:
        return self.inner.newest_date()

    def fetched_at(self, url: str) -> datetime | None:
        return self.inner.fetched_at(url)


class LogCapture(logging.Handler):
    """Keeps every log message of the current item, for the PII and leak checks."""

    def __init__(self) -> None:
        super().__init__(logging.INFO)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.lines.append(record.getMessage())
        except Exception:  # pragma: no cover - a bad log call must not stop the eval
            self.lines.append(str(record.msg))

    def take(self) -> str:
        text = "\n".join(self.lines)
        self.lines.clear()
        return text


# ---------------------------------------------------------------- throttle


@dataclass
class Throttle:
    """Keeps eval traffic under `share` of Groq's per-minute limits and stops before the
    daily reserve (implementation plan, task 6.2 notes)."""

    tpm: int
    rpm: int
    tpd: int
    share: float = 0.8
    reserve: int = 20_000
    day_tokens: int = 0
    clock: Callable[[], float] = time.time
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    _events: deque[tuple[float, int, int]] = field(default_factory=deque)

    def _minute(self, now: float) -> tuple[int, int]:
        while self._events and self._events[0][0] <= now - 60:
            self._events.popleft()
        return sum(e[1] for e in self._events), sum(e[2] for e in self._events)

    def day_allows(self, est_tokens: int) -> bool:
        return self.day_tokens + est_tokens + self.reserve <= self.tpd

    async def wait_for_room(self, est_tokens: int, est_requests: int) -> float:
        """Sleep until the minute window has room; returns how long it waited."""
        waited = 0.0
        while True:
            now = self.clock()
            tokens, requests = self._minute(now)
            fits_tokens = tokens + est_tokens <= self.tpm * self.share or not self._events
            fits_requests = requests + est_requests <= self.rpm * self.share
            if fits_tokens and fits_requests:
                return waited
            pause = max(self._events[0][0] + 60 - now, 0.5)
            await self.sleep(pause)
            waited += pause

    def record(self, tokens: int, requests: int) -> None:
        self._events.append((self.clock(), tokens, requests))
        self.day_tokens += tokens


# ---------------------------------------------------------------- results


@dataclass
class ItemRecord:
    id: str
    category: str
    scored: bool
    repeat: int
    status: str  # done | skipped
    type: str | None = None
    text: str | None = None
    citation_url: str | None = None
    footer: str | None = None
    reason: str | None = None
    scheme: str | None = None
    grades: dict[str, dict[str, Any]] = field(default_factory=dict)
    passed: bool = False
    latency_s: float | None = None  # last turn, end to end through send_message
    llm_calls: int = 0  # whole item
    tokens: dict[str, int] = field(default_factory=dict)  # whole item
    model_turns: int = 0  # turns that reached the model
    top_score: float | None = None
    gap: float | None = None
    low_confidence: bool | None = None
    retrieved: list[str] = field(default_factory=list)  # last turn, chunk IDs in order
    recall_hit: bool | None = None
    log_leak: bool = False  # a turn's text appeared in the logs
    finished_at: str = ""

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, default=str)


class RateLimitedStop(Exception):
    def __init__(self, retry_after_s: float | None) -> None:
        super().__init__(f"rate limited (retry_after={retry_after_s})")
        self.retry_after_s = retry_after_s


# ---------------------------------------------------------------- context


def load_context(sf: sessionmaker[Session], retriever: RetrieverLike) -> Context:
    with sf() as session:
        facts = {
            (r.scheme, r.fact_key): r.value_text
            for r in session.scalars(
                select(SchemeFact).where(
                    SchemeFact.verified.is_(True), SchemeFact.value_text.is_not(None)
                )
            )
            if r.value_text
        }
        page_dates: dict[str, date] = {}
        for row in session.execute(select(ChunkRow.scheme, ChunkRow.as_of_date)):
            page_dates.setdefault(row.scheme, row.as_of_date)
    return Context(facts=facts, page_dates=page_dates, newest=retriever.newest_date())


def expected_chunks(item: GoldenItem) -> set[str]:
    e = item.expect
    if e.scheme is None or "answer" not in e.type:
        return set()
    return {f"{_SOURCE_OF[e.scheme]}#{FACT_SECTIONS[k]}" for k in e.fact_keys if k in FACT_SECTIONS}


def model_turns(item: GoldenItem) -> int:
    return sum(needs_model(t.user) for t in item.turns)


# ---------------------------------------------------------------- one item


@dataclass
class Harness:
    services: Services
    store: ThreadStore
    llm: SpyLLM
    retriever: SpyRetriever
    ctx: Context
    capture: LogCapture

    async def run_item(self, item: GoldenItem, repeat: int) -> ItemRecord:
        thread = await asyncio.to_thread(self.store.create, EVAL_CLIENT_ID)
        rec = ItemRecord(item.id, item.category, item.scored, repeat, "done")
        usage_before = dict(self.llm.usage)
        calls_at_start = self.llm.calls
        self.capture.take()
        try:
            for n, turn in enumerate(item.turns):
                last = n == len(item.turns) - 1
                rows_before = await asyncio.to_thread(self.store.count_messages)
                calls_before = self.llm.calls
                self.retriever.last = None
                with request_context(f"req_eval_{item.id}_{repeat}_{n}"):
                    started = time.monotonic()
                    result = await send_message(
                        self.services, thread.id, EVAL_CLIENT_ID, turn.user
                    )
                    latency = time.monotonic() - started
                resp = result.response
                if self.llm.calls > calls_before:
                    rec.model_turns += 1
                if self.llm.inner is None and self.llm.calls > calls_before:
                    rec.status = "skipped"  # --no-llm and this item needs the model
                    return rec
                if resp.type == "busy" and "busy" not in item.expect.type:
                    retry = self.llm.last.retry_after_s if self.llm.last else None
                    raise RateLimitedStop(retry)
                if not last:
                    continue
                rows_after = await asyncio.to_thread(self.store.count_messages)
                retrieval = self.retriever.last
                cited_dates: set[date] = set()
                if retrieval is not None and resp.citation is not None:
                    cited_dates = {
                        d
                        for i in retrieval.items
                        if i.source_url == resp.citation.url and (d := footer_date(i))
                    }
                log_text = self.capture.take()
                observed = Observed(
                    type=resp.type,
                    text=resp.text,
                    citation_url=resp.citation.url if resp.citation else None,
                    footer=resp.footer,
                    last_updated=resp.last_updated,
                    reason=resp.reason,
                    scheme=resp.scheme,
                    llm_calls=self.llm.calls - calls_before,
                    rows_delta=rows_after - rows_before,
                    log_text=log_text,
                    retrieved_dates=frozenset(cited_dates),
                )
                graded = grade(item, observed, self.ctx)
                rec.type, rec.text, rec.reason, rec.scheme = (
                    resp.type,
                    resp.text,
                    resp.reason,
                    resp.scheme,
                )
                rec.citation_url, rec.footer = observed.citation_url, resp.footer
                rec.grades = {k: {"ok": g.ok, "detail": g.detail} for k, g in graded.grades.items()}
                rec.passed = graded.passed
                rec.latency_s = round(latency, 3)
                rec.log_leak = any(
                    len(t.user) >= 8 and t.user in log_text for t in item.turns
                )
                if retrieval is not None:
                    rec.top_score = retrieval.top_score
                    rec.gap = retrieval.gap
                    rec.low_confidence = retrieval.low_confidence
                    rec.retrieved = [i.item_id for i in retrieval.items if i.kind == "chunk"]
                want = expected_chunks(item)
                if want:
                    rec.recall_hit = want <= set(rec.retrieved[:5])
        finally:
            await asyncio.to_thread(self.store.delete, thread.id, EVAL_CLIENT_ID)
            rec.llm_calls = self.llm.calls - calls_at_start
            rec.tokens = {k: self.llm.usage[k] - usage_before[k] for k in USAGE_KEYS}
        rec.finished_at = datetime.now(UTC).isoformat(timespec="seconds")
        return rec


# ---------------------------------------------------------------- run


@dataclass
class RunOptions:
    run_dir: Path
    repeat: int = 1
    no_llm: bool = False
    share: float = 0.8
    reserve: int = 20_000
    max_wait_s: float = 90.0
    max_rate_retries: int = 3


def build_harness(
    s: Settings, eval_db_url: str, no_llm: bool, max_wait_s: float
) -> tuple[Harness, Settings]:
    from growwbuddy.llm.client import GroqClient
    from growwbuddy.retrieval.embed import make_embedder
    from growwbuddy.retrieval.vector import VectorIndex
    from growwbuddy.store.db import init_db, make_engine, make_session_factory

    # The eval keeps its own reserve (Throttle), so the client may use the whole limit;
    # it may also wait out a longer retry-after than chat does.
    es = s.model_copy(update={"groq_headroom": 1.0, "llm_max_retry_wait_s": max_wait_s})
    index_engine = make_engine(es.db_url)
    init_db(index_engine)
    index_sf = make_session_factory(index_engine)
    eval_engine = make_engine(eval_db_url)
    init_db(eval_engine)
    store = ThreadStore(make_session_factory(eval_engine))

    real = Retriever(
        VectorIndex(index_sf, es.embed_model),
        make_embedder(es),
        index_sf,
        min_score=es.retrieval_min_score,
        min_gap=es.retrieval_min_gap,
    )
    retriever = SpyRetriever(real)
    llm = SpyLLM(None if no_llm else GroqClient(es))
    pipeline = Pipeline(
        llm,
        retriever,
        history_turns=es.history_turns,
        fetched_at=retriever.fetched_at,
        stale_days=es.stale_days,
    )
    services = Services(
        store=store,
        pipeline=pipeline,
        ready=real.ready,
        newest_date=real.newest_date,
        history_turns=es.history_turns,
        limiter=None,  # the eval is one client sending many messages on purpose
    )
    capture = LogCapture()
    harness = Harness(services, store, llm, retriever, load_context(index_sf, retriever), capture)
    return harness, es


def read_progress(run_dir: Path) -> list[ItemRecord]:
    path = run_dir / "progress.jsonl"
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            out.append(ItemRecord(**json.loads(line)))
    return out


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:12]


def _git_commit() -> str | None:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None


def run_metadata(s: Settings, ctx: Context, item_file: Path, opts: RunOptions) -> dict[str, Any]:
    return {
        "model": s.model,
        "reasoning_effort": s.reasoning_effort,
        "embed_model": s.embed_model,
        "retrieval_min_score": s.retrieval_min_score,
        "retrieval_min_gap": s.retrieval_min_gap,
        "prompts": {p.name: _sha(p) for p in sorted(PROMPTS_DIR.glob("*.md"))},
        "corpus_as_of": {k: v.isoformat() for k, v in sorted(ctx.page_dates.items())},
        "item_file": item_file.name,
        "item_file_sha": _sha(item_file),
        "git_commit": _git_commit(),
        "repeat": opts.repeat,
        "no_llm": opts.no_llm,
        "started_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }


def order_items(items: Sequence[GoldenItem]) -> list[GoldenItem]:
    """Free (rule-handled) items first, then model items, each in file order."""
    return sorted(items, key=lambda i: model_turns(i) > 0)


async def run_items(
    harness: Harness,
    items: Sequence[GoldenItem],
    opts: RunOptions,
    throttle: Throttle,
    *,
    on_record: Callable[[ItemRecord], None],
    done: set[tuple[str, int]],
    say: Callable[[str], None] = print,
) -> str:
    """Run every (item, repeat) not in `done`. Returns why it stopped: complete | budget |
    rate_limited."""
    measured: list[float] = []
    for repeat in range(opts.repeat):
        for item in order_items(items):
            if (item.id, repeat) in done:
                continue
            turns_needing_model = model_turns(item)
            if turns_needing_model and opts.no_llm:
                continue
            est = 0
            if turns_needing_model:
                per_turn = (
                    sum(measured) / len(measured) if measured else DEFAULT_TOKENS_PER_MODEL_TURN
                )
                est = round(per_turn * turns_needing_model)
                if not throttle.day_allows(est):
                    say(
                        f"stopping: today's eval tokens ({throttle.day_tokens}) plus this "
                        f"item (~{est}) would eat into the {throttle.reserve}-token reserve"
                    )
                    return "budget"
                waited = await throttle.wait_for_room(est, 2 * turns_needing_model + 1)
                if waited:
                    say(f"  (waited {waited:.0f}s for the per-minute budget)")
            for attempt in range(opts.max_rate_retries + 1):
                try:
                    rec = await harness.run_item(item, repeat)
                    break
                except RateLimitedStop as e:
                    throttle.record(0, 1)
                    wait = e.retry_after_s
                    if wait is None or wait > opts.max_wait_s or attempt == opts.max_rate_retries:
                        say(
                            f"stopping: rate limited on {item.id} (retry-after {wait}); "
                            "resume later with --resume"
                        )
                        return "rate_limited"
                    say(f"  rate limited on {item.id}; waiting {wait:.0f}s")
                    await asyncio.sleep(wait)
            billable = rec.tokens.get("total_tokens", 0) - rec.tokens.get("cached_tokens", 0)
            throttle.record(max(billable, 0), rec.llm_calls)
            if rec.model_turns and rec.status == "done":
                measured.append(rec.tokens.get("total_tokens", 0) / rec.model_turns)
            on_record(rec)
            mark = "skip" if rec.status == "skipped" else ("PASS" if rec.passed else "FAIL")
            failed = [k for k, g in rec.grades.items() if not g["ok"]]
            extra = f"  failed: {', '.join(failed)}" if failed else ""
            say(f"{mark} {item.id:<12} r{repeat} {rec.type or '-':<13} {rec.latency_s or 0:5.2f}s{extra}")
    return "complete"


def configure_eval_logging(run_dir: Path, capture: LogCapture) -> None:
    """All app logs at INFO go to the capture and to `eval.log` (JSON, no message text by
    design: EV-6-12 re-checks that); only warnings are echoed to the console."""
    install_record_factory()
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    file_handler = logging.FileHandler(run_dir / "eval.log", encoding="utf-8")
    file_handler.setFormatter(JsonFormatter())
    console = logging.StreamHandler()
    console.setLevel(logging.WARNING)
    console.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    root.handlers[:] = [capture, file_handler, console]
    for noisy in ("httpx", "httpcore", "sentence_transformers", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
