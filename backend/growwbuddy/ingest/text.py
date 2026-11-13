"""Text clean-up helpers shared by the parsers (EC-ING-12, EC-ING-19)."""

from __future__ import annotations

import re

from bs4 import BeautifulSoup

_MOJIBAKE_MARKERS = ("â\u0080", "â‚", "Ã", "Â")
_SPACES = re.compile(r"[\u00a0\u2007\u2009\u202f\s]+")
PLACEHOLDERS = frozenset({"", "na", "n/a", "-", "--", "–", "—", "null", "none", "0"})


_NON_ASCII_RUN = re.compile(r"[^\x00-\x7f]+")


def _repair_run(m: re.Match[str]) -> str:
    run = m.group(0)
    for codec in ("cp1252", "latin-1"):
        try:
            return run.encode(codec).decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            continue
    return run


def fix_mojibake(s: str) -> str:
    """Repair UTF-8 text that was wrongly decoded as cp1252/latin-1 (e.g. 'â‚¹' -> '₹').

    Each run of non-ASCII characters is repaired on its own, so correct characters
    elsewhere in the string (a real '₹' or a non-breaking space) are left alone.
    """
    if not any(m in s for m in _MOJIBAKE_MARKERS):
        return s
    return _NON_ASCII_RUN.sub(_repair_run, s)


def clean_text(s: str) -> str:
    """Strip HTML tags, repair encoding and normalise whitespace."""
    s = fix_mojibake(s)
    if "<" in s and ">" in s:
        s = BeautifulSoup(s, "html.parser").get_text(" ")
    return _SPACES.sub(" ", s).strip()


def is_placeholder(value: object) -> bool:
    """True for values that mean 'not available': None, '', 'NA', '–', '--', '0', ...

    Note: "Nil" (without a full stop) is a real exit-load value and is NOT a placeholder.
    """
    if value is None:
        return True
    if isinstance(value, (int, float)):
        return value == 0
    if isinstance(value, str):
        return clean_text(value).lower() in PLACEHOLDERS
    return False
