"""Shared strings and identifiers used by the API, UI (via /api/meta), validator and tests.

Keep user-facing fixed text here so there is exactly one copy of it.
"""

from __future__ import annotations

from datetime import date
from typing import Final

DISCLAIMER: Final = "Facts-only. No investment advice."

# The five schemes in scope (implementation plan, Phase 1). Order is the display order.
SCHEME_KEYS: Final[tuple[str, ...]] = (
    "hdfc-mid-cap",
    "hdfc-small-cap",
    "hdfc-large-cap",
    "hdfc-silver-etf-fof",
    "hdfc-multi-cap",
)

SCHEME_NAMES: Final[dict[str, str]] = {
    "hdfc-mid-cap": "HDFC Mid Cap Fund",
    "hdfc-small-cap": "HDFC Small Cap Fund",
    "hdfc-large-cap": "HDFC Large Cap Fund",
    "hdfc-silver-etf-fof": "HDFC Silver ETF FoF",
    "hdfc-multi-cap": "HDFC Multi Cap Fund",
}

# Only domain that may be fetched, indexed or cited.
ALLOWED_DOMAIN: Final = "groww.in"

# The fixed corpus (implementation plan, Phase 1): (source_id, scheme_key, url).
# corpus/sources.yaml must match this exactly; changing it means changing the plan first.
SOURCES: Final[tuple[tuple[str, str, str], ...]] = (
    (
        "groww-hdfc-mid-cap",
        "hdfc-mid-cap",
        "https://groww.in/mutual-funds/hdfc-mid-cap-fund-direct-growth",
    ),
    (
        "groww-hdfc-small-cap",
        "hdfc-small-cap",
        "https://groww.in/mutual-funds/hdfc-small-cap-fund-direct-growth",
    ),
    (
        "groww-hdfc-large-cap",
        "hdfc-large-cap",
        "https://groww.in/mutual-funds/hdfc-large-cap-fund-direct-growth",
    ),
    (
        "groww-hdfc-silver-etf-fof",
        "hdfc-silver-etf-fof",
        "https://groww.in/mutual-funds/hdfc-silver-etf-fof-direct-growth",
    ),
    (
        "groww-hdfc-multi-cap",
        "hdfc-multi-cap",
        "https://groww.in/mutual-funds/hdfc-multi-cap-fund-direct-growth",
    ),
)

# The six SEBI riskometer levels.
RISKOMETER_LEVELS: Final[tuple[str, ...]] = (
    "Low",
    "Low to Moderate",
    "Moderate",
    "Moderately High",
    "High",
    "Very High",
)

FOOTER_TEMPLATE: Final = "Last updated from sources: {date}"
FOOTER_DATE_FORMAT: Final = "%d %b %Y"  # e.g. "31 Oct 2026" (edge case EC-VAL-11)
# Added to the footer, outside the answer body, when the cited source hasn't been fetched
# in GROWWBUDDY_STALE_DAYS (task 6.6, EC-RET-08, EC-VAL-09).
STALE_NOTE: Final = "(source may be out of date)"

PII_INPUT_HINT: Final = (
    "Please don't share PAN, Aadhaar, account numbers, OTPs, email or phone numbers."
)


# Shown as clickable chips on an empty thread (task 4.6, 5.5). Each one is answered from
# a verified scheme fact, so a first click always gets a real answer.
EXAMPLE_QUESTIONS: Final[tuple[str, str, str]] = (
    "What is the expense ratio of HDFC Mid Cap Fund?",
    "What is the exit load of HDFC Small Cap Fund?",
    "What is the minimum SIP amount for HDFC Large Cap Fund?",
)

# Thread title until the first factual question is answered (task 4.7, EC-THR-15).
NEW_CHAT_TITLE: Final = "New chat"
THREAD_TITLE_MAX_CHARS: Final = 60


def expense_ratio_phrase(base: str | None, total: str | None) -> str | None:
    """The expense ratio as Groww publishes it: two figures (task 2.7, EC-FCT-10).

    Groww's data has `base_expense_ratio` (shown on the page as "Exclusive of GST &
    statutory charges") and `expense_ratio` (the figure including them). The page has shown
    either one, so answers always label and give both, and match whichever is displayed.
    Used by the chunk text and the facts-table context, so the two can't disagree.
    """
    if base and total:
        return f"{base} excluding GST and statutory charges, and {total} including them"
    if base:
        return f"{base} excluding GST and statutory charges"
    if total:
        return f"{total} including GST and statutory charges"
    return None


def format_footer(as_of: date) -> str:
    """Build the answer footer from the cited source's as-of date."""
    return FOOTER_TEMPLATE.format(date=as_of.strftime(FOOTER_DATE_FORMAT))
