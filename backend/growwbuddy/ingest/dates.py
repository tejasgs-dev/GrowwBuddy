"""Date parsing and as-of date selection (task 1.8; EC-ING-16, 17, 18)."""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import date, datetime, timedelta

# Indian DD/MM/YYYY order is assumed for numeric dates (EC-ING-18).
_FORMATS = (
    "%d-%b-%Y",  # 31-Oct-2026
    "%d %b %Y",  # 31 Oct 2026
    "%d-%B-%Y",  # 31-October-2026
    "%d %B %Y",  # 31 October 2026
    "%d/%m/%Y",  # 31/10/2026
    "%d-%m-%Y",  # 31-10-2026
    "%d.%m.%Y",  # 31.10.2026
    "%Y-%m-%d",  # 2026-10-31
)
_ISO_PREFIX = re.compile(r"^(\d{4}-\d{2}-\d{2})[T ]")
_ORDINAL = re.compile(r"(\d{1,2})(st|nd|rd|th)\b", re.IGNORECASE)

MAX_AGE = timedelta(days=365)
# Pages state dates in IST while we record fetches in UTC; allow one day of slack.
FUTURE_SLACK = timedelta(days=1)


def parse_date(text: str | None) -> date | None:
    """Parse one date string in any supported format; None if it isn't a date."""
    if not text:
        return None
    s = " ".join(text.strip().split())
    m = _ISO_PREFIX.match(s)
    if m:
        s = m.group(1)
    s = _ORDINAL.sub(r"\1", s)
    for fmt in _FORMATS:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def is_plausible(d: date, reference: date) -> bool:
    """A data date must not be in the future or more than a year older than `reference`."""
    return reference - MAX_AGE <= d <= reference + FUTURE_SLACK


def choose_as_of(candidates: Iterable[str | None], fetched_at: datetime) -> tuple[date, str]:
    """Pick the first plausible candidate date, else fall back to the fetch date.

    Candidates must be passed most-preferred first (the NAV/data date, never a launch date).
    Returns (date, origin) where origin is "page" or "fetched_at".
    """
    reference = fetched_at.date()
    for c in candidates:
        d = parse_date(c)
        if d is not None and is_plausible(d, reference):
            return d, "page"
    return reference, "fetched_at"
