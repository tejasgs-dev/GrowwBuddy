"""Fixed reply templates (task 3.2, architecture §8)."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from growwbuddy.constants import SCHEME_KEYS, SCHEME_NAMES
from growwbuddy.pipeline import refusals
from growwbuddy.pipeline.validator import has_advice, has_return_figure, has_url

SCHEMED: list[Callable[[str | None], str]] = [
    refusals.advisory,
    refusals.comparison,
    refusals.performance,
    refusals.not_found,
    refusals.plan_not_covered,
    refusals.fallback,
]
FIXED = [
    refusals.out_of_scope(),
    refusals.elss(),
    refusals.personal(),
    refusals.internals(),
    refusals.PII,
    refusals.BUSY,
    refusals.UNAVAILABLE,
    refusals.TOO_LONG,
    refusals.WELCOME,
    refusals.THANKS,
    refusals.which_scheme(),
    refusals.one_scheme_at_a_time(["hdfc-mid-cap", "hdfc-small-cap"]),
]


def _all_texts() -> list[str]:
    return [f(s) for f in SCHEMED for s in (None, *SCHEME_KEYS)] + FIXED


@pytest.mark.parametrize("text", _all_texts())
def test_templates_have_no_urls_or_figures(text: str) -> None:
    assert text.strip()
    assert not has_url(text)
    assert not has_return_figure(text)


def test_busy_text_is_exact() -> None:
    assert refusals.BUSY == "GrowwBuddy is busy right now. Please try again shortly."


def test_named_scheme_appears_in_scheme_replies() -> None:
    for key in SCHEME_KEYS:
        assert SCHEME_NAMES[key] in refusals.performance(key)
        assert SCHEME_NAMES[key] in refusals.not_found(key)


def test_out_of_scope_lists_all_five_schemes() -> None:
    text = refusals.out_of_scope()
    assert all(SCHEME_NAMES[k] in text for k in SCHEME_KEYS)


def test_advisory_refusal_gives_no_advice_itself() -> None:
    # It mentions a SEBI-registered adviser, which is fine; it must not recommend anything.
    assert not has_advice(refusals.advisory("hdfc-mid-cap").replace("adviser", ""))
