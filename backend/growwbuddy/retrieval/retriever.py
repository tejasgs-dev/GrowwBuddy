"""Retriever (task 3.8, architecture §6.3 as changed by the Phase 2 data research).

1. **Verified facts first.** If the query asks for a known fact (TER, exit load, min SIP,
   ...) and the resolved scheme has a *verified* value, it becomes a top-priority context
   item (EC-RET-04: the verified fact wins; EC-RET-05: unverified facts are never used).
2. **Vector search filtered to the resolved scheme**, top 5. No BM25, RRF or doc-type boost
   (one doc type, and the hybrid didn't help). Never across schemes (EC-RET-03): with no
   scheme the caller asks which scheme instead.
3. **Low-confidence exit** when no fact matched and the best score is below
   `min_score`, or the gap between the top two scores is below `min_gap`. Both cutoffs are
   unset until the Phase 6 eval tunes them for `e5-small-v2` (task 6.3).

The retriever also tells the formatter each scheme's Groww page and its data date, used
for refusal links and footers (task 1.2, EC-VAL-12).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Literal, TypeVar

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from growwbuddy.constants import (
    FOOTER_DATE_FORMAT,
    SCHEME_NAMES,
    SOURCES,
    expense_ratio_phrase,
)
from growwbuddy.retrieval.embed import Embedder
from growwbuddy.retrieval.vector import (
    IndexedChunk,
    IndexNotReadyError,
    MixedIndexError,
    VectorIndex,
)
from growwbuddy.store.models import SchemeFact, SourceState

log = logging.getLogger(__name__)

TOP_K = 5
FACT_ID_PREFIX = "fact:"

# Query wording -> scheme_facts key. Checked in order; a query can match several.
_FACT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "exit_load",
        re.compile(r"\bexit\b|\bel\b|redemption (charge|fee)|withdrawal (charge|penalty)", re.I),
    ),
    (
        "expense_ratio_direct",
        re.compile(r"expense|\bter\b|annual fee|management fee|fund charges", re.I),
    ),
    ("min_sip", re.compile(r"\bsips?\b", re.I)),
    (
        "min_lumpsum",
        re.compile(r"lump ?sum|one[- ]time|minimum (first |initial )?(investment|amount)", re.I),
    ),
    ("benchmark", re.compile(r"benchmark|\bindex\b", re.I)),
    ("riskometer", re.compile(r"risk", re.I)),
    ("fund_manager", re.compile(r"manager|manages|managed by|\bfm\b", re.I)),
    ("lock_in", re.compile(r"lock[- ]?in", re.I)),
)
# Every fact the question names is looked up: they are merged into one citable item
# (_merge_facts), so dropping one would make the whole answer fail. The 3-sentence limit
# still applies; the prompt asks the model to fit them in.
MAX_FACTS = len(_FACT_PATTERNS)

_D = TypeVar("_D", date, datetime)

_TER_TOTAL = "expense_ratio_direct"  # including GST and statutory charges
_TER_BASE = "expense_ratio_base"  # excluding them


def _oldest(values: list[_D | None]) -> _D | None:
    present = [v for v in values if v is not None]
    return min(present) if present else None


def _merge_facts(items: list[ContextItem]) -> list[ContextItem]:
    """Several facts asked in one question become one context item.

    An answer cites exactly one item, and every number it states must be in that item
    (validator, EC-GEN-10). With one item per fact, "exit load and expense ratio of small
    cap" can't pass: whichever item it cites, the other fact's numbers are missing. All of
    a scheme's facts come from its one Groww page, so one merged item keeps the single
    citation honest. Its dates are the oldest of the parts, so the footer never claims
    data is newer than it is.
    """
    if len(items) < 2 or len({(i.scheme, i.source_url) for i in items}) != 1:
        return items
    first = items[0]
    keys = "+".join(i.item_id.rsplit(":", 1)[1] for i in items)
    return [
        ContextItem(
            item_id=f"{FACT_ID_PREFIX}{first.scheme}:{keys}",
            kind="fact",
            scheme=first.scheme,
            text=" ".join(i.text for i in items),
            source_url=first.source_url,
            as_of_date=_oldest([i.as_of_date for i in items]),
            fact_as_on=_oldest([i.fact_as_on for i in items]),
            fetched_at=_oldest([i.fetched_at for i in items]),
        )
    ]


def _value_text(key: str, found: dict[str, SchemeFact]) -> str:
    """A fact's value as given to the model. The expense ratio gives both labelled figures
    (or the one that is verified), matching the chunk text."""
    if key == _TER_TOTAL:
        total, base = found.get(_TER_TOTAL), found.get(_TER_BASE)
        phrase = expense_ratio_phrase(
            base.value_text if base else None, total.value_text if total else None
        )
        if phrase:
            return phrase
    row = found[key]
    return row.value_text or ""


_FACT_LABELS: dict[str, str] = {
    "expense_ratio_direct": "Expense ratio (TER)",  # gives both figures, see _value_text
    "exit_load": "Exit load",
    "min_sip": "Minimum SIP amount",
    "min_lumpsum": "Minimum first (lump sum) investment",
    "benchmark": "Benchmark index",
    "riskometer": "Riskometer classification",
    "fund_manager": "Fund managers",
    "lock_in": "Lock-in period",
}


def fact_keys_for(query: str) -> list[str]:
    return [key for key, rx in _FACT_PATTERNS if rx.search(query)][:MAX_FACTS]


@dataclass(frozen=True)
class ContextItem:
    """One labelled item the generator can cite by `item_id`."""

    item_id: str
    kind: Literal["fact", "chunk"]
    scheme: str
    text: str
    source_url: str
    as_of_date: date | None  # the page's data date
    fact_as_on: date | None  # the fact's own date, when it has one
    fetched_at: datetime | None
    score: float | None = None


@dataclass(frozen=True)
class PageInfo:
    scheme: str
    url: str
    title: str
    as_of_date: date | None
    fetched_at: datetime | None


@dataclass(frozen=True)
class Retrieval:
    items: list[ContextItem] = field(default_factory=list)
    low_confidence: bool = False
    top_score: float | None = None
    gap: float | None = None

    def get(self, item_id: str) -> ContextItem | None:
        return next((i for i in self.items if i.item_id == item_id), None)


def page_title(scheme: str) -> str:
    return f"{SCHEME_NAMES[scheme]} Direct Growth – Groww"


class Retriever:
    def __init__(
        self,
        index: VectorIndex,
        embedder: Embedder,
        session_factory: sessionmaker[Session],
        *,
        min_score: float | None = None,
        min_gap: float | None = None,
        k: int = TOP_K,
    ) -> None:
        self.index = index
        self.embedder = embedder
        self._session_factory = session_factory
        self.min_score = min_score
        self.min_gap = min_gap
        self.k = k

    # ------------------------------------------------------------ retrieval

    def retrieve(self, query: str, scheme: str) -> Retrieval:
        """Facts + top-k chunks for one scheme. Raises IndexNotReadyError / MixedIndexError."""
        self.index.refresh_if_changed()
        by_id = {c.chunk_id: c for c in self.index.chunks()}
        facts = self._facts(query, scheme, by_id)

        hits = self.index.search(self.embedder.embed_query(query), scheme, self.k)
        chunks = [
            ContextItem(
                item_id=h.chunk.chunk_id,
                kind="chunk",
                scheme=h.chunk.scheme,
                text=h.chunk.text,
                source_url=h.chunk.source_url,
                as_of_date=h.chunk.as_of_date,
                fact_as_on=h.chunk.fact_as_on,
                fetched_at=h.chunk.fetched_at,
                score=h.score,
            )
            for h in hits
        ]
        top = hits[0].score if hits else None
        gap = hits[0].score - hits[1].score if len(hits) >= 2 else None
        low = not facts and self._is_low_confidence(top, gap)
        log.info(
            "retrieval scheme=%s facts=%d chunks=%d top=%s gap=%s low_confidence=%s",
            scheme,
            len(facts),
            len(chunks),
            None if top is None else round(top, 3),
            None if gap is None else round(gap, 3),
            low,
        )
        return Retrieval(items=[*facts, *chunks], low_confidence=low, top_score=top, gap=gap)

    def _is_low_confidence(self, top: float | None, gap: float | None) -> bool:
        if top is None:
            return True
        if self.min_score is not None and top < self.min_score:
            return True
        return self.min_gap is not None and gap is not None and gap < self.min_gap

    def _facts(self, query: str, scheme: str, by_id: dict[str, IndexedChunk]) -> list[ContextItem]:
        keys = fact_keys_for(query)
        if not keys:
            return []
        # An expense-ratio question also loads the figure excluding GST, so one context
        # item (the one the answer cites) states both and the number check passes.
        wanted = [*keys, _TER_BASE] if _TER_TOTAL in keys else keys
        with self._session_factory() as session:
            rows = session.scalars(
                select(SchemeFact).where(
                    SchemeFact.scheme == scheme,
                    SchemeFact.fact_key.in_(wanted),
                    SchemeFact.verified.is_(True),
                    SchemeFact.value_text.is_not(None),
                )
            ).all()
        found = {r.fact_key: r for r in rows}
        items = []
        for key in keys:
            row = found.get(key)
            if key == _TER_TOTAL:
                row = row or found.get(_TER_BASE)
            if row is None or not row.value_text:
                continue
            value = _value_text(key, found)
            chunk = by_id.get(f"{row.source_id}#{row.section}") if row.section else None
            # The fact's own date (e.g. TER "as on"), when the page gives one.
            own_date = chunk.fact_as_on if chunk else None
            sep = "," if key == _TER_TOTAL else ""
            as_on = f"{sep} as on {own_date.strftime(FOOTER_DATE_FORMAT)}" if own_date else ""
            label = _FACT_LABELS.get(key, key.replace("_", " ").capitalize())
            items.append(
                ContextItem(
                    item_id=f"{FACT_ID_PREFIX}{scheme}:{key}",
                    kind="fact",
                    scheme=scheme,
                    text=f"{label} of {SCHEME_NAMES[scheme]} Direct Growth: {value}{as_on}.",
                    source_url=row.source_url,
                    as_of_date=chunk.as_of_date if chunk else row.as_of_date,
                    # scheme_facts.as_of_date is the fact's own date, else the page's.
                    fact_as_on=row.as_of_date,
                    fetched_at=chunk.fetched_at if chunk else None,
                )
            )
        return _merge_facts(items)

    # ------------------------------------------------------------ page links

    def page(self, scheme: str) -> PageInfo | None:
        """The scheme's Groww page and data date; the URL alone if the index is down."""
        url = next((u for _, k, u in SOURCES if k == scheme), None)
        if url is None:
            return None
        chunk = None
        try:
            chunk = next((c for c in self.index.chunks() if c.scheme == scheme), None)
        except (IndexNotReadyError, MixedIndexError):  # still link the page, no date
            log.warning("index unavailable; page date unknown for %s", scheme)
        return PageInfo(
            scheme=scheme,
            url=url,
            title=page_title(scheme),
            as_of_date=chunk.as_of_date if chunk else None,
            fetched_at=chunk.fetched_at if chunk else None,
        )

    def fetched_at(self, url: str) -> datetime | None:
        """When the source behind `url` was last fetched successfully (staleness, task 6.6).

        None when unknown: no fetch recorded, or not one of the five sources.
        """
        source_id = next((sid for sid, _, u in SOURCES if u == url), None)
        if source_id is None:
            return None
        with self._session_factory() as session:
            state = session.get(SourceState, source_id)
            when = state.fetched_at if state else None
        if when is not None and when.tzinfo is None:
            when = when.replace(tzinfo=UTC)  # SQLite drops the zone; stored as UTC
        return when

    def ready(self) -> bool:
        """True when a usable index is loaded (EC-RET-06): built, non-empty, right model."""
        try:
            self.index.refresh_if_changed()
        except (IndexNotReadyError, MixedIndexError):
            return False
        return len(self.index) > 0

    def newest_date(self) -> date | None:
        try:
            dates = [c.as_of_date for c in self.index.chunks() if c.as_of_date]
        except (IndexNotReadyError, MixedIndexError):
            return None
        return max(dates) if dates else None
