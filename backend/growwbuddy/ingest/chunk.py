"""Turn a parsed page into chunks: one chunk per parsed section (tasks 2.1, 2.2).

The Phase 2 data research found sections are 14-41 words and self-contained (each names
its scheme), so there is no splitting or overlap. Each chunk stores two texts:

- `text`: the plain section, shown to the model and the user.
- `embed_text`: `<header>\\n<text>`, only ever embedded. The header adds scheme aliases
  and section keywords (see corpus/retrieval_headers.yaml).
"""

from __future__ import annotations

import hashlib
import math
import re
from datetime import date, datetime

from pydantic import BaseModel, ConfigDict

from growwbuddy.ingest.parse_groww import ParsedPage
from growwbuddy.retrieval.headers import RetrievalHeaders, headers

DOC_TYPE = "scheme_page"
MAX_CHUNK_TOKENS = 256

# Parsed field whose own date (`as_on`) applies to the section. Today only the expense
# ratio has one; the rest fall back to the page's as_of_date in the footer (task 3.11).
SECTION_FACT_FIELD: dict[str, str] = {
    "expense_ratio": "expense_ratio",
    "exit_load": "exit_load",
    "minimum_investment": "min_sip",
    "riskometer": "riskometer",
    "benchmark": "benchmark",
    "fund_manager": "fund_managers",
    "lock_in": "lock_in",
    "about_the_fund": "objective",
    "tax": "tax",
    "category": "category",
    "launch_date": "launch_date",
    "registrar": "registrar",
    "stamp_duty": "stamp_duty",
}


class ChunkTooLongError(ValueError):
    """A chunk exceeds the token guard; the parser produced something unexpected."""


class Chunk(BaseModel):
    model_config = ConfigDict(frozen=True)

    chunk_id: str
    source_id: str
    scheme: str
    section: str
    title: str
    doc_type: str
    source_url: str
    as_of_date: date
    fact_as_on: date | None
    fetched_at: datetime
    text: str
    embed_text: str
    content_sha256: str


_PIECES = re.compile(r"\w+|[^\w\s]")


def estimate_tokens(text: str) -> int:
    """Conservative upper estimate of WordPiece tokens without loading a tokenizer.

    Takes the larger of "one token per word or symbol" and "one token per 3 characters"
    (English WordPiece averages ~4 characters per token), so it errs on the high side.
    """
    return max(len(_PIECES.findall(text)), math.ceil(len(text) / 3))


def chunk_id(source_id: str, section: str) -> str:
    """Stable across re-ingests: `<source_id>#<section>`, never a running number."""
    return f"{source_id}#{section}"


def chunk_page(page: ParsedPage, h: RetrievalHeaders | None = None) -> list[Chunk]:
    h = h or headers()
    chunks: list[Chunk] = []
    seen: set[str] = set()
    for sec in page.sections:
        if sec.name in seen:
            raise ValueError(f"{page.source_id}: duplicate section '{sec.name}'")
        seen.add(sec.name)
        embed_text = f"{h.header(page.scheme_key, sec.name, sec.title)}\n{sec.text}"
        tokens = estimate_tokens(embed_text)
        if tokens > MAX_CHUNK_TOKENS:
            raise ChunkTooLongError(
                f"{chunk_id(page.source_id, sec.name)} is ~{tokens} tokens "
                f"(limit {MAX_CHUNK_TOKENS}); check the parser output"
            )
        field = page.fields.get(SECTION_FACT_FIELD.get(sec.name, ""))
        chunks.append(
            Chunk(
                chunk_id=chunk_id(page.source_id, sec.name),
                source_id=page.source_id,
                scheme=page.scheme_key,
                section=sec.name,
                title=sec.title,
                doc_type=DOC_TYPE,
                source_url=page.url,
                as_of_date=page.as_of_date,
                fact_as_on=field.as_on if field else None,
                fetched_at=page.fetched_at,
                text=sec.text,
                embed_text=embed_text,
                content_sha256=hashlib.sha256(sec.text.encode("utf-8")).hexdigest(),
            )
        )
    return chunks
