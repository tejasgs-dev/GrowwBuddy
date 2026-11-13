"""Build the response object (task 3.11, architecture §6.6).

Footer date for an answer: the cited item's own fact date (`fact_as_on`, e.g. the TER's
"as on" date) when present, otherwise the cited source's `as_of_date`, otherwise
`fetched_at` (EC-VAL-10). Format "31 Oct 2026" (EC-VAL-11).

Template replies link the scheme's Groww page and use that page's date. A refusal with no
known scheme has no link and uses the newest corpus date (EC-VAL-12). Replies in
`NO_LINK_TYPES` (PII, greeting, clarification, busy, unavailable) have neither.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from typing import Protocol

from growwbuddy.constants import format_footer
from growwbuddy.pipeline.types import NO_LINK_TYPES, Citation, Response, ResponseType
from growwbuddy.retrieval.retriever import ContextItem, PageInfo, page_title


class PageLookup(Protocol):
    def page(self, scheme: str) -> PageInfo | None: ...

    def newest_date(self) -> date | None: ...


def footer_date(item: ContextItem) -> date | None:
    if item.fact_as_on is not None:
        return item.fact_as_on
    if item.as_of_date is not None:
        return item.as_of_date
    if item.fetched_at is not None:
        return item.fetched_at.date()
    return None


def _page_date(page: PageInfo) -> date | None:
    if page.as_of_date is not None:
        return page.as_of_date
    return page.fetched_at.date() if page.fetched_at else None


def answer_response(sentences: Sequence[str], cited: ContextItem) -> Response:
    when = footer_date(cited)
    return Response(
        type="answer",
        text=" ".join(s.strip() for s in sentences),
        citation=Citation(url=cited.source_url, title=page_title(cited.scheme)),
        last_updated=when,
        footer=format_footer(when) if when else None,
        scheme=cited.scheme,
    )


def template_response(
    type_: ResponseType,
    text: str,
    *,
    scheme: str | None = None,
    reason: str | None = None,
    pages: PageLookup | None = None,
) -> Response:
    """A fixed reply, with the scheme's Groww page as its one link when a scheme is known."""
    if type_ in NO_LINK_TYPES:
        return Response(type=type_, text=text, reason=reason, scheme=scheme)

    page = pages.page(scheme) if (pages and scheme) else None
    when = _page_date(page) if page else None
    if page is not None and when is not None:
        return Response(
            type=type_,
            text=text,
            citation=Citation(url=page.url, title=page.title),
            last_updated=when,
            footer=format_footer(when),
            reason=reason,
            scheme=scheme,
        )
    # No scheme, or the page's date is unknown because the index is down: no link, and
    # `scheme` stays None so the validator's linkless-refusal exception applies.
    newest = pages.newest_date() if pages else None
    return Response(
        type=type_,
        text=text,
        last_updated=newest,
        footer=format_footer(newest) if newest else None,
        reason=reason,
    )
