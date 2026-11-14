"""Extract structured scheme facts from a parsed page (tasks 2.7-2.9).

Values come from the parsed `fields` (already structured in Phase 1), so the rule-based
extractors parse known formats rather than scrape pages. Exit-load wording is the one
field with free-form text: known wordings are handled by rules, and anything else goes to
an LLM extractor that runs **only at ingestion**, never at query time (task 2.8).

Every value is sanity-checked (fact_checks.py). A value that fails, an LLM failure, or a
page that disagrees with itself (EC-FCT-10) is stored with `verified = false`.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, replace
from datetime import date

from growwbuddy.ingest.fact_checks import check_fact, parse_percent
from growwbuddy.ingest.parse_groww import ParsedPage, normalise_riskometer
from growwbuddy.ingest.text import clean_text
from growwbuddy.llm.client import StructuredLLM

ExtractedBy = str  # "rule" | "llm" | "manual"


@dataclass(frozen=True)
class FactSpec:
    fact_key: str
    field: str  # parsed field it comes from
    section: str  # chunk section that holds the evidence


# §5.3 fact keys. lock_in is extracted only if the page states one (EC-FCT-08).
FACT_SPECS: tuple[FactSpec, ...] = (
    # Two published figures (see constants.expense_ratio_phrase): including and excluding
    # GST and statutory charges. Both are core; answers give both.
    FactSpec("expense_ratio_direct", "expense_ratio", "expense_ratio"),
    FactSpec("expense_ratio_base", "expense_ratio_base", "expense_ratio"),
    FactSpec("exit_load", "exit_load", "exit_load"),
    FactSpec("min_sip", "min_sip", "minimum_investment"),
    FactSpec("min_lumpsum", "min_lumpsum", "minimum_investment"),
    FactSpec("benchmark", "benchmark", "benchmark"),
    FactSpec("riskometer", "riskometer", "riskometer"),
    FactSpec("lock_in", "lock_in", "lock_in"),
    FactSpec("fund_manager", "fund_managers", "fund_manager"),
)
FACT_KEYS = tuple(s.fact_key for s in FACT_SPECS)
CORE_FACT_KEYS = (
    "expense_ratio_direct",
    "expense_ratio_base",
    "exit_load",
    "min_sip",
    "benchmark",
    "riskometer",
)

# check_error of an exit load whose wording the rules don't recognise when no LLM is
# configured (the daily refresh runs without GROQ_API_KEY). Stored unverified; the refresh
# lists it for review instead of failing the core-facts gate (tasks 7.4, 7.9).
NO_LLM_EXIT_LOAD = "wording not recognised by rules and no LLM configured"

# Headline figure -> its check-only copy from the page's TER history (EC-FCT-10).
_HISTORY_FIELD = {
    "expense_ratio_direct": ("expense_ratio_history", "TER"),
    "expense_ratio_base": ("expense_ratio_base_history", "base TER"),
}


@dataclass(frozen=True)
class ExtractedFact:
    scheme: str
    fact_key: str
    value: str | None
    extracted_by: ExtractedBy
    check_error: str | None
    evidence: str | None
    section: str
    as_of_date: date
    source_id: str
    source_url: str

    @property
    def verified(self) -> bool:
        return self.value is not None and self.check_error is None


# ---------------------------------------------------------------- exit load

_EXIT_LOAD_RULES = (
    # "Exit load of 1% if redeemed within 1 year." / "Exit load of 1%, if redeemed within 15 days"
    re.compile(
        r"^(?:exit load of )?\d+(?:\.\d+)?%,? if redeemed within \d+ (?:days?|months?|years?)$",
        re.IGNORECASE,
    ),
    re.compile(r"^(?:nil|no exit load|none)$", re.IGNORECASE),
)


def rule_exit_load(text: str) -> str | None:
    """The wording as the page states it, if it's a known single-tier or nil format."""
    s = clean_text(text).rstrip(".").strip()
    return s if any(r.match(s) for r in _EXIT_LOAD_RULES) else None


EXIT_LOAD_SCHEMA_NAME = "exit_load_extraction"
EXIT_LOAD_SCHEMA = {
    "type": "object",
    "properties": {
        "stated": {"type": "boolean"},
        "exit_load": {"type": "string"},
    },
    "required": ["stated", "exit_load"],
    "additionalProperties": False,
}
EXIT_LOAD_SYSTEM = (
    "You extract the exit load of a mutual fund scheme from text taken from its Groww "
    "scheme page. Copy the exit load wording as written, including every tier, and keep "
    "all numbers exactly as they appear. Do not add, compute, explain or round anything. "
    'If the text does not state an exit load, return stated=false and exit_load="". '
    'If it states there is no exit load, return the wording, for example "Nil".'
)

_NUMBER = re.compile(r"\d+(?:[.,]\d+)*")


def numbers_in(text: str) -> set[str]:
    return {n.replace(",", "") for n in _NUMBER.findall(text)}


def grounding_error(output: str, source: str) -> str | None:
    """Every number the LLM returns must appear in the source text (EC-FCT-11)."""
    extra = numbers_in(output) - numbers_in(source)
    return f"LLM value has numbers not in the source: {sorted(extra)}" if extra else None


async def llm_exit_load(text: str, llm: StructuredLLM) -> tuple[str | None, str | None]:
    """Returns (value, error). On any failure nothing is stored and the fact is flagged."""
    result = await llm.complete_json(
        system=EXIT_LOAD_SYSTEM,
        user=f"Text:\n{text}",
        schema_name=EXIT_LOAD_SCHEMA_NAME,
        schema=EXIT_LOAD_SCHEMA,
        max_tokens=512,
    )
    if not result.ok or result.data is None:
        return None, f"LLM extraction failed ({result.error}); review manually"
    stated, value = result.data.get("stated"), result.data.get("exit_load")
    if not isinstance(stated, bool) or not isinstance(value, str):
        return None, "LLM output doesn't match the schema; review manually"
    value = clean_text(value)
    if not stated or not value:
        return None, "LLM found no exit load in text that has one; review manually"
    if err := grounding_error(value, text):
        return None, err
    return value, None


# ---------------------------------------------------------------- page


def _section_text(page: ParsedPage, name: str) -> str | None:
    return next((s.text for s in page.sections if s.name == name), None)


def extract_facts(page: ParsedPage, llm: StructuredLLM | None = None) -> list[ExtractedFact]:
    """All facts the page states. Missing fields produce no row ("not found" answers)."""
    out: list[ExtractedFact] = []
    for spec in FACT_SPECS:
        field = page.fields.get(spec.field)
        if field is None:
            continue
        evidence = _section_text(page, spec.section) or field.value
        extracted_by: ExtractedBy = "rule"
        error: str | None = None
        value: str | None

        if spec.fact_key == "exit_load":
            value = rule_exit_load(field.value)
            if value is None:
                extracted_by = "llm"
                if llm is None:
                    error = NO_LLM_EXIT_LOAD
                else:
                    value, error = asyncio.run(llm_exit_load(field.value, llm))
        elif spec.fact_key == "riskometer":
            value = normalise_riskometer(field.value)
        else:
            value = clean_text(field.value)

        if error is None:
            error = check_fact(spec.fact_key, value)

        if spec.fact_key in _HISTORY_FIELD and error is None:
            history_field, label = _HISTORY_FIELD[spec.fact_key]
            other = page.fields.get(history_field)
            if other is not None and other.value != value:
                when = f" as on {other.as_on.isoformat()}" if other.as_on else ""
                error = (
                    f"page disagrees with itself: headline {label} {value} vs "
                    f"{label} history {other.value}{when}"
                )
                evidence = f"{evidence}\n[{label} history{when}: {other.value}]"

        out.append(
            ExtractedFact(
                scheme=page.scheme_key,
                fact_key=spec.fact_key,
                value=value,
                extracted_by=extracted_by,
                check_error=error,
                evidence=evidence,
                section=spec.section,
                as_of_date=field.as_on or page.as_of_date,
                source_id=page.source_id,
                source_url=page.url,
            )
        )
    return _check_ter_pair(out)


def _check_ter_pair(facts: list[ExtractedFact]) -> list[ExtractedFact]:
    """The figure excluding GST and statutory charges can't exceed the one including them.

    If it does, the two fields were probably swapped or misread, so both are flagged.
    """
    by_key = {f.fact_key: f for f in facts}
    total, base = by_key.get("expense_ratio_direct"), by_key.get("expense_ratio_base")
    if total is None or base is None or total.value is None or base.value is None:
        return facts
    t, b = parse_percent(total.value), parse_percent(base.value)
    if t is None or b is None or b <= t:
        return facts
    msg = f"base TER {base.value} (excluding GST) is above total TER {total.value}"
    return [
        replace(f, check_error=f.check_error or msg) if f in (total, base) else f for f in facts
    ]
