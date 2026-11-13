"""Retrieval headers and scheme alias resolution (task 2.4)."""

from __future__ import annotations

from pathlib import Path

import pytest

from growwbuddy.retrieval.headers import (
    HeadersError,
    RetrievalHeaders,
    load_headers,
    resolve_scheme,
)


def test_headers_file_covers_every_scheme_and_section(
    retrieval_headers: RetrievalHeaders,
) -> None:
    from growwbuddy.ingest.chunk import SECTION_FACT_FIELD

    assert set(SECTION_FACT_FIELD) <= set(retrieval_headers.sections)


@pytest.mark.parametrize(
    ("text", "scheme"),
    [
        ("exit load mid cap", "hdfc-mid-cap"),
        ("What is the TER of HDFC Small Cap Fund?", "hdfc-small-cap"),
        ("largecap min sip", "hdfc-large-cap"),
        ("silver fof benchmark", "hdfc-silver-etf-fof"),
        ("multi-cap riskometer", "hdfc-multi-cap"),
        ("what is the exit load", None),  # no scheme (EC-RET-03)
        ("mid cap vs small cap", None),  # two schemes: not resolved here
        ("HDFC cap fund", None),  # ambiguous (EC-INT-20)
    ],
)
def test_resolve_scheme(text: str, scheme: str | None, retrieval_headers: RetrievalHeaders) -> None:
    assert resolve_scheme(text, retrieval_headers) == scheme


def test_alias_shared_by_two_schemes_is_rejected(tmp_path: Path) -> None:
    p = tmp_path / "h.yaml"
    p.write_text(
        "schemes:\n"
        "  hdfc-mid-cap: {aliases: [cap]}\n"
        "  hdfc-small-cap: {aliases: [cap]}\n"
        "  hdfc-large-cap: {aliases: []}\n"
        "  hdfc-silver-etf-fof: {aliases: []}\n"
        "  hdfc-multi-cap: {aliases: []}\n"
        "sections: {}\n",
        encoding="utf-8",
    )
    with pytest.raises(HeadersError, match="alias"):
        load_headers(p)


def test_missing_scheme_is_rejected(tmp_path: Path) -> None:
    p = tmp_path / "h.yaml"
    p.write_text("schemes:\n  hdfc-mid-cap: {aliases: []}\nsections: {}\n", encoding="utf-8")
    with pytest.raises(HeadersError):
        load_headers(p)
