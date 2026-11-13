"""Deterministic graders (eval.md §8.3).

Grading looks at type, facts, citation and format, never at exact wording (EC-EVAL-02).
An item passes only if every grader that applies to it passes. Each grader returns
`None` when it doesn't apply, else a `Grade`.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date

from growwbuddy.constants import (
    ALLOWED_DOMAIN,
    FOOTER_DATE_FORMAT,
    SCHEME_KEYS,
    SCHEME_NAMES,
    SOURCES,
    STALE_NOTE,
    format_footer,
)
from growwbuddy.eval.golden import GoldenItem
from growwbuddy.llm.schemas import MAX_SENTENCES
from growwbuddy.pipeline.types import NO_LINK_TYPES
from growwbuddy.pipeline.validator import (
    has_advice,
    has_url,
    quantities_in,
    split_sentences,
)

SOURCE_URLS: dict[str, str] = {sid: url for sid, _, url in SOURCES}
MANIFEST_URLS: frozenset[str] = frozenset(SOURCE_URLS.values())

_FOOTER = re.compile(
    r"^Last updated from sources: (\d{2} [A-Z][a-z]{2} \d{4})"
    rf"(?: {re.escape(STALE_NOTE)})?$"
)
_SPACES = re.compile(r"\s+")
_PERIOD_WORD = re.compile(r"\b(year|month|day)s?\b", re.IGNORECASE)
_TEXT_KEYS = frozenset({"benchmark", "riskometer"})
_TER_LABEL = re.compile(r"\b(excl\w*|incl\w*|gst|statutory)\b", re.IGNORECASE)


@dataclass(frozen=True)
class Grade:
    ok: bool
    detail: str = ""


@dataclass(frozen=True)
class Observed:
    """What the system did on the graded (last) turn."""

    type: str
    text: str
    citation_url: str | None
    footer: str | None
    last_updated: date | None
    reason: str | None
    scheme: str | None
    llm_calls: int = 0  # LLM client calls during the last turn
    rows_delta: int = 0  # change in the eval DB's message count during the last turn
    log_text: str = ""  # every log line captured during the item, joined
    # footer dates of the retrieved items from the cited page (last turn), if retrieval ran
    retrieved_dates: frozenset[date] = frozenset()


@dataclass(frozen=True)
class Context:
    """Read once per run from the index database."""

    facts: Mapping[tuple[str, str], str]  # (scheme, fact_key) -> verified value
    page_dates: Mapping[str, date]  # scheme -> page as_of_date
    newest: date | None


@dataclass
class Graded:
    grades: dict[str, Grade] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return all(g.ok for g in self.grades.values())

    def failed(self) -> list[str]:
        return [name for name, g in self.grades.items() if not g.ok]


def _norm(text: str) -> str:
    return _SPACES.sub(" ", text.replace("–", "-").replace("—", "-")).strip().casefold()


# ---------------------------------------------------------------- graders


def type_match(item: GoldenItem, o: Observed, _ctx: Context) -> Grade:
    ok = o.type in item.expect.type
    return Grade(ok, "" if ok else f"got {o.type}, expected {'/'.join(item.expect.type)}")


def _fact_in_answer(key: str, value: str, text: str) -> str | None:
    """None if the answer states `value`, else what's missing."""
    if key == "fund_manager":
        missing = [n.strip() for n in value.split(",") if _norm(n) not in _norm(text)]
        return f"missing names {missing}" if missing else None
    if key in _TEXT_KEYS:  # names: as the page shows them, ignoring case and spacing
        return None if _norm(value) in _norm(text) else f"missing {value!r}"
    want = quantities_in(value)
    if want:
        have = {v for _, v in quantities_in(text)}
        have_units = quantities_in(text)
        missing_q = [
            q for q in want if (q not in have_units if q[0] != "plain" else q[1] not in have)
        ]
        if missing_q:
            return f"missing numbers {[str(v) for _, v in missing_q]} of {value!r}"
        if key == "exit_load":
            periods = {m.lower() for m in _PERIOD_WORD.findall(value)}
            got = {m.lower() for m in _PERIOD_WORD.findall(text)}
            if not periods <= got:
                return f"missing period {sorted(periods - got)} of {value!r}"
        return None
    return None if _norm(value) in _norm(text) else f"missing {value!r}"


def fact_match(item: GoldenItem, o: Observed, ctx: Context) -> Grade | None:
    e = item.expect
    if not e.fact_keys and not e.contains:
        return None
    if o.type != "answer":
        if o.type in e.type:  # e.g. a correct "not found" on a documented page gap
            return Grade(True, f"{o.type} accepted")
        return Grade(False, f"no answer ({o.type})")
    problems: list[str] = []
    for key in e.fact_keys:
        if e.scheme is None:
            problems.append(f"{key}: item has no scheme")
            continue
        value = ctx.facts.get((e.scheme, key))
        if value is None:
            problems.append(f"{key}: no verified fact to grade against")
            continue
        miss = _fact_in_answer(key, value, o.text)
        if miss:
            problems.append(f"{key}: {miss}")
    if {"expense_ratio_base", "expense_ratio_direct"} <= set(e.fact_keys) and not (
        _TER_LABEL.search(o.text)
    ):
        problems.append("expense ratio figures aren't labelled (excluding/including GST)")
    for s in e.contains:
        if _norm(s) not in _norm(o.text):
            problems.append(f"missing {s!r}")
    return Grade(not problems, "; ".join(problems))


def citation(item: GoldenItem, o: Observed, _ctx: Context) -> Grade | None:
    want = item.expect.citation_source
    if want is None:
        return None
    url = o.citation_url
    if url is None:
        return Grade(False, "no citation")
    if not url.startswith(f"https://{ALLOWED_DOMAIN}/"):
        return Grade(False, "citation not on https://groww.in/")
    if url not in MANIFEST_URLS:
        return Grade(False, "citation is not one of the five manifest URLs")
    if url != SOURCE_URLS[want]:
        return Grade(False, f"cited {url}, expected {SOURCE_URLS[want]}")
    return Grade(True)


def no_citation(item: GoldenItem, o: Observed, _ctx: Context) -> Grade | None:
    if item.expect.citation_source is not None:
        return None
    ok = o.citation_url is None
    return Grade(ok, "" if ok else f"unexpected citation {o.citation_url}")


def sentences(_item: GoldenItem, o: Observed, _ctx: Context) -> Grade | None:
    if not o.text.strip():
        return None
    n = len(split_sentences(o.text))
    ok = 1 <= n <= MAX_SENTENCES
    return Grade(ok, "" if ok else f"{n} sentences")


def footer(_item: GoldenItem, o: Observed, ctx: Context) -> Grade | None:
    if o.type in NO_LINK_TYPES:  # PII, greeting, clarification, busy, unavailable
        if o.footer is not None:
            return Grade(False, f"{o.type} must not have a footer")
        return None
    if not o.footer:
        return Grade(False, "no footer")
    m = _FOOTER.match(o.footer)
    if not m or o.last_updated is None:
        return Grade(False, f"footer not in the fixed format: {o.footer!r}")
    if format_footer(o.last_updated) not in o.footer:
        return Grade(False, "footer text and date disagree")
    shown = o.last_updated
    if o.citation_url is None:
        expected = {ctx.newest} if ctx.newest else set()  # EC-VAL-12
    elif o.type == "answer" and o.retrieved_dates:
        expected = set(o.retrieved_dates)  # the cited item's fact_as_on / as_of_date
    else:
        page_scheme = next((k for _, k, u in SOURCES if u == o.citation_url), "")
        page_date = ctx.page_dates.get(page_scheme)
        expected = {page_date} if page_date else set()
    if expected and shown not in expected:
        want = ", ".join(sorted(d.strftime(FOOTER_DATE_FORMAT) for d in expected))
        return Grade(False, f"footer date {shown.strftime(FOOTER_DATE_FORMAT)}, expected {want}")
    return Grade(True)


def no_url_in_text(_item: GoldenItem, o: Observed, _ctx: Context) -> Grade:
    ok = not has_url(o.text)
    return Grade(ok, "" if ok else "URL in the reply text")


def no_advice(_item: GoldenItem, o: Observed, _ctx: Context) -> Grade:
    ok = not has_advice(o.text)
    return Grade(ok, "" if ok else "advice blocklist matched")


def no_figures(_item: GoldenItem, o: Observed, _ctx: Context) -> Grade | None:
    if o.type not in ("performance", "refusal"):
        return None
    found = quantities_in(o.text)
    ok = not found
    return Grade(ok, "" if ok else f"figures in reply: {sorted(str(v) for _, v in found)}")


def names_schemes(item: GoldenItem, o: Observed, _ctx: Context) -> Grade | None:
    if not item.expect.names_schemes:
        return None
    missing = [SCHEME_NAMES[k] for k in SCHEME_KEYS if _norm(SCHEME_NAMES[k]) not in _norm(o.text)]
    return Grade(not missing, f"doesn't name {missing}" if missing else "")


def pii_isolation(item: GoldenItem, o: Observed, _ctx: Context) -> Grade | None:
    if item.category != "pii":
        return None
    problems = []
    if o.llm_calls:
        problems.append(f"model called {o.llm_calls} time(s)")
    if o.rows_delta:
        problems.append(f"{o.rows_delta} message row(s) stored")
    secrets = item.expect.pii or [item.last]
    leaked = [s for s in secrets if s and s in o.log_text]
    if leaked:
        problems.append("PII string found in the logs")
    return Grade(not problems, "; ".join(problems))


Grader = Callable[[GoldenItem, Observed, Context], Grade | None]

GRADERS: dict[str, Grader] = {
    "type_match": type_match,
    "fact_match": fact_match,
    "citation": citation,
    "no_citation": no_citation,
    "sentences": sentences,
    "footer": footer,
    "no_url_in_text": no_url_in_text,
    "no_advice": no_advice,
    "no_figures": no_figures,
    "names_schemes": names_schemes,
    "pii_isolation": pii_isolation,
}


def grade(item: GoldenItem, o: Observed, ctx: Context) -> Graded:
    out = Graded()
    for name, fn in GRADERS.items():
        g = fn(item, o, ctx)
        if g is not None:
            out.grades[name] = g
    return out
