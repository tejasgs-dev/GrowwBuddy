"""Parse a Groww scheme page into clean, sectioned text (tasks 1.6, 1.7).

Groww pages are Next.js apps that embed the fund's data as JSON in
`<script id="__NEXT_DATA__">` at `props.pageProps.mfServerSideData`. We read only an
allow-list of fields from it. Everything else is dropped: returns, NAV values, ratings,
peer comparison, holdings, "funds managed" lists, AMC contact details (EC-ING-14, 15).

Field choices verified against the visible pages (see docs/corpus-notes.md):
- Riskometer comes from `return_stats[0].risk` (matches the risk pill shown on the page).
  `nfo_risk` is the launch-time value and is stale, so it is ignored.
- Fund managers come from `fund_manager_details` (current team). The top-level
  `fund_manager` string can be stale, so it is ignored.
- Of `category_info`, only `tax_impact` is used (shown on the page under "Tax").
  Its other fields describe a different category and are ignored.

A page without that JSON (bot-check page, error page, layout change) or for a different
scheme is rejected with ParseError, so it is never indexed (EC-ING-02, 05).
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from pydantic import BaseModel

from growwbuddy.constants import RISKOMETER_LEVELS, expense_ratio_phrase
from growwbuddy.ingest.dates import choose_as_of, parse_date
from growwbuddy.ingest.manifest import SourceEntry
from growwbuddy.ingest.text import clean_text, is_placeholder

PARSE_METHOD = "next_data_json"

_NEXT_DATA = re.compile(
    r'<script[^>]*id="__NEXT_DATA__"[^>]*>(?P<json>.*?)</script>', re.DOTALL | re.IGNORECASE
)

# Scalar fields copied as-is from mfServerSideData (everything else is dropped).
_KEPT_SCALARS = (
    "scheme_name",
    "search_id",
    "plan_type",
    "scheme_type",
    "amc",
    "fund_house",
    "category",
    "sub_category",
    "launch_date",
    "registrar_agent",
    "stamp_duty",
    "description",
    "expense_ratio",  # including GST and statutory charges
    "base_expense_ratio",  # excluding them (the figure Groww's page box shows today)
    "exit_load",
    "min_sip_investment",
    "min_investment_amount",
    "mini_additional_investment",
    "benchmark",
    "benchmark_name",
    "nav_date",
)

# Facts every page should state (implementation plan, Phase 1 exit check).
CORE_FIELDS = (
    "expense_ratio",
    "expense_ratio_base",
    "exit_load",
    "min_sip",
    "riskometer",
    "benchmark",
)
OPTIONAL_FIELDS = (
    "min_lumpsum",
    "min_additional",
    "fund_managers",
    "lock_in",
    "tax",
    "objective",
    "category",
    "launch_date",
    "registrar",
    "stamp_duty",
)


class ParseError(Exception):
    """The page isn't a valid scheme page for the expected source."""


class Fact(BaseModel):
    value: str
    as_on: date | None = None


class Section(BaseModel):
    name: str
    title: str
    text: str


class ParsedPage(BaseModel):
    source_id: str
    scheme_key: str
    url: str
    scheme_name: str
    parse_method: str
    fetched_at: datetime
    as_of_date: date
    as_of_origin: Literal["page", "fetched_at"]
    content_sha256: str
    fields: dict[str, Fact]
    sections: list[Section]
    missing: list[str]


# ---------------------------------------------------------------- extraction


def _load_next_data(html: str) -> dict[str, Any]:
    m = _NEXT_DATA.search(html)
    if not m:
        raise ParseError("no embedded fund data found (bot-check page or layout change)")
    try:
        data = json.loads(m.group("json"))
        fund = data["props"]["pageProps"]["mfServerSideData"]
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        raise ParseError(f"embedded fund data has an unexpected shape: {e}") from e
    if not isinstance(fund, dict):
        raise ParseError("embedded fund data is not an object")
    return fund


def _latest_expense(fund: dict[str, Any]) -> tuple[str | None, object, object]:
    """Date, total and base expense ratio of the newest `historic_fund_expense` entry.

    The values are second copies of the two headline figures, used only to catch the page
    disagreeing with itself (EC-FCT-10).
    """
    history = [
        h
        for h in fund.get("historic_fund_expense") or []
        if isinstance(h, dict) and h.get("as_on_date")
    ]
    if not history:
        return None, None, None
    latest = max(history, key=lambda h: str(h["as_on_date"]))
    return str(latest["as_on_date"]), latest.get("expense_ratio"), latest.get("base_expense_ratio")


def extract_fund_data(html: str, source: SourceEntry) -> dict[str, Any]:
    """Return only the allow-listed fund content from the page.

    This dict is what gets hashed for change detection (task 1.5), so ads, layout,
    returns and NAV values don't trigger a re-parse.
    """
    raw = _load_next_data(html)

    if raw.get("search_id") != source.slug:
        raise ParseError(
            f"page is for '{raw.get('search_id')}', expected '{source.slug}' "
            "(scheme renamed, merged or redirected?)"
        )
    if raw.get("plan_type") != "Direct" or raw.get("scheme_type") != "Growth":
        raise ParseError(
            f"expected Direct - Growth, got {raw.get('plan_type')} - {raw.get('scheme_type')}"
        )
    if "HDFC" not in str(raw.get("fund_house", "")):
        raise ParseError(f"unexpected fund house: {raw.get('fund_house')}")

    fund: dict[str, Any] = {k: raw.get(k) for k in _KEPT_SCALARS}

    return_stats = raw.get("return_stats") or []
    first_stats = return_stats[0] if return_stats and isinstance(return_stats[0], dict) else {}
    fund["riskometer"] = first_stats.get("risk")

    managers = raw.get("fund_manager_details") or []
    fund["fund_managers"] = [
        m["person_name"] for m in managers if isinstance(m, dict) and m.get("person_name")
    ]

    (
        fund["expense_ratio_as_on"],
        fund["expense_ratio_history_latest"],
        fund["base_expense_ratio_history_latest"],
    ) = _latest_expense(raw)

    lock_in = raw.get("lock_in") or {}
    fund["lock_in"] = {k: lock_in.get(k) for k in ("years", "months", "days")}

    category_info = raw.get("category_info") or {}
    fund["tax_impact"] = category_info.get("tax_impact")

    return fund


def content_hash(fund: dict[str, Any]) -> str:
    canonical = json.dumps(fund, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- formatting


def format_inr(amount: int | float) -> str:
    """Format rupees with Indian digit grouping: 100000 -> '₹1,00,000'."""
    whole = round(amount)
    s = str(abs(whole))
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        groups: list[str] = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        s = ",".join([*groups, tail])
    return f"{'-' if whole < 0 else ''}₹{s}"


def format_percent(value: object) -> str | None:
    try:
        d = Decimal(str(value).strip().rstrip("%"))
    except (InvalidOperation, ValueError):
        return None
    return f"{d.quantize(Decimal('0.01'))}%"


def normalise_riskometer(value: str) -> str:
    """Map wording variants to one of the six SEBI levels; unknown values pass through."""
    s = clean_text(value)
    s = re.sub(r"\s+(riskometer|risk)$", "", s, flags=re.IGNORECASE)
    for level in RISKOMETER_LEVELS:
        if s.lower() == level.lower():
            return level
    return s


def _format_lock_in(lock_in: dict[str, Any]) -> str | None:
    parts = []
    for unit in ("years", "months", "days"):
        n = lock_in.get(unit)
        if isinstance(n, int) and n > 0:
            parts.append(f"{n} {unit[:-1] if n == 1 else unit}")
    return " ".join(parts) or None


def _long_date(d: date) -> str:
    return d.strftime("%d %b %Y")


def _sentence(text: str) -> str:
    return text if text.endswith((".", "!", "?")) else text + "."


# ---------------------------------------------------------------- page building


def _build_facts(fund: dict[str, Any]) -> tuple[dict[str, Fact], list[str]]:
    facts: dict[str, Fact] = {}

    def put(name: str, value: str | None, as_on: date | None = None) -> None:
        if value is not None and not is_placeholder(value):
            facts[name] = Fact(value=value, as_on=as_on)

    ter_as_on = parse_date(fund.get("expense_ratio_as_on"))
    for name, key in (
        ("expense_ratio", "expense_ratio"),
        ("expense_ratio_base", "base_expense_ratio"),
        # Check-only copies from the TER history (EC-FCT-10); never shown or indexed.
        ("expense_ratio_history", "expense_ratio_history_latest"),
        ("expense_ratio_base_history", "base_expense_ratio_history_latest"),
    ):
        if not is_placeholder(fund.get(key)):
            put(name, format_percent(fund[key]), ter_as_on)
    if isinstance(fund.get("exit_load"), str):
        put("exit_load", clean_text(fund["exit_load"]))
    for name, key in (
        ("min_sip", "min_sip_investment"),
        ("min_lumpsum", "min_investment_amount"),
        ("min_additional", "mini_additional_investment"),
    ):
        v = fund.get(key)
        if isinstance(v, (int, float)) and not is_placeholder(v) and v > 0:
            put(name, format_inr(v))
    if isinstance(fund.get("riskometer"), str):
        put("riskometer", normalise_riskometer(fund["riskometer"]))
    if isinstance(fund.get("benchmark_name"), str):
        put("benchmark", clean_text(fund["benchmark_name"]))
    if isinstance(fund.get("benchmark"), str):
        put("benchmark_short", clean_text(fund["benchmark"]))
    if fund.get("fund_managers"):
        put("fund_managers", ", ".join(clean_text(n) for n in fund["fund_managers"]))
    put("lock_in", _format_lock_in(fund.get("lock_in") or {}))
    if isinstance(fund.get("tax_impact"), str):
        put("tax", clean_text(fund["tax_impact"]))
    if isinstance(fund.get("description"), str):
        put("objective", clean_text(fund["description"]))
    category = " – ".join(
        clean_text(str(fund[k])) for k in ("category", "sub_category") if fund.get(k)
    )
    put("category", category or None)
    launch = parse_date(fund.get("launch_date"))
    put("launch_date", _long_date(launch) if launch else None)
    for name, key in (
        ("registrar", "registrar_agent"),
        ("stamp_duty", "stamp_duty"),
        ("fund_house", "fund_house"),
    ):
        if isinstance(fund.get(key), str):
            put(name, clean_text(fund[key]))

    missing = [f for f in (*CORE_FIELDS, *OPTIONAL_FIELDS) if f not in facts]
    return facts, missing


def _build_sections(name: str, f: dict[str, Fact]) -> list[Section]:
    """Readable, self-contained text per section; each names the scheme so chunks stand alone."""
    sections: list[Section] = []

    def add(key: str, title: str, text: str) -> None:
        sections.append(Section(name=key, title=title, text=text))

    # Fund details: one section per fact (data research: splitting the mixed
    # `fund_details` section raised its retrieval top-1 from 77% to 93%).
    if "category" in f:
        house = f" from {f['fund_house'].value}" if "fund_house" in f else ""
        add(
            "category",
            "Category",
            f"{name} is the Direct plan, Growth option of a scheme{house}. "
            f"Its category is {f['category'].value}.",
        )
    if "launch_date" in f:
        add("launch_date", "Launch date", f"The launch date of {name} is {f['launch_date'].value}.")
    if "registrar" in f:
        add(
            "registrar",
            "Registrar",
            f"The registrar and transfer agent of {name} is {f['registrar'].value}.",
        )
    if "stamp_duty" in f:
        add("stamp_duty", "Stamp duty", f"Stamp duty for {name}: {f['stamp_duty'].value}.")

    total, base = f.get("expense_ratio"), f.get("expense_ratio_base")
    phrase = expense_ratio_phrase(base.value if base else None, total.value if total else None)
    if phrase:
        dated = total or base  # both share the TER history date
        when = f", as on {_long_date(dated.as_on)}" if dated and dated.as_on else ""
        add(
            "expense_ratio",
            "Expense ratio",
            f"The expense ratio (TER) of {name} is {phrase}{when}.",
        )
    if "exit_load" in f:
        add("exit_load", "Exit load", _sentence(f"Exit load of {name}: {f['exit_load'].value}"))
    mins = []
    if "min_sip" in f:
        mins.append(f"The minimum SIP amount for {name} is {f['min_sip'].value}.")
    if "min_lumpsum" in f:
        mins.append(f"The minimum first (lump sum) investment is {f['min_lumpsum'].value}.")
    if "min_additional" in f:
        mins.append(f"The minimum additional investment is {f['min_additional'].value}.")
    if mins:
        add("minimum_investment", "Minimum investment / SIP", " ".join(mins))
    if "riskometer" in f:
        add(
            "riskometer",
            "Riskometer",
            f"The riskometer classification of {name} is {f['riskometer'].value} risk.",
        )
    if "benchmark" in f:
        text = f"The benchmark index of {name} is {f['benchmark'].value}"
        short = f.get("benchmark_short")
        if short and short.value != f["benchmark"].value:
            text += f" ({short.value})"
        add("benchmark", "Benchmark", text + ".")
    if "fund_managers" in f:
        add(
            "fund_manager",
            "Fund manager",
            f"The fund managers of {name} are {f['fund_managers'].value}.",
        )
    if "lock_in" in f:
        add("lock_in", "Lock-in", f"{name} has a lock-in period of {f['lock_in'].value}.")
    if "objective" in f:
        add(
            "about_the_fund",
            "About the fund",
            _sentence(f"Investment objective of {name}: {f['objective'].value}"),
        )
    if "tax" in f:
        add("tax", "Tax", _sentence(f"Tax on redemption of {name}: {f['tax'].value}"))
    return sections


def build_parsed_page(
    fund: dict[str, Any], source: SourceEntry, fetched_at: datetime
) -> ParsedPage:
    scheme_name = clean_text(str(fund.get("scheme_name") or ""))
    if not scheme_name:
        raise ParseError("scheme name missing from page data")
    facts, missing = _build_facts(fund)
    as_of, origin = choose_as_of([fund.get("nav_date")], fetched_at)
    return ParsedPage(
        source_id=source.id,
        scheme_key=source.scheme,
        url=source.url,
        scheme_name=scheme_name,
        parse_method=PARSE_METHOD,
        fetched_at=fetched_at,
        as_of_date=as_of,
        as_of_origin="page" if origin == "page" else "fetched_at",
        content_sha256=content_hash(fund),
        fields=facts,
        sections=_build_sections(scheme_name, facts),
        missing=missing,
    )


def parse_page(html: str, source: SourceEntry, fetched_at: datetime) -> ParsedPage:
    return build_parsed_page(extract_fund_data(html, source), source, fetched_at)
