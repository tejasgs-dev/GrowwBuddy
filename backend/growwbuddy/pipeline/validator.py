"""Output validator: the deterministic last gate (task 3.10, architecture §6.5).

`validate_answer` checks a model answer against the context it was given:

| Check | On failure |
|---|---|
| 1-3 sentences (abbreviation- and decimal-aware; list items count as sentences) | retry |
| exactly one citation, present in the retrieved set, on groww.in | retry |
| no URLs or website names in the text | retry |
| every number appears in the cited item, compared by value and unit (EC-GEN-10) | retry |
| cited item is the resolved scheme; no other scheme named (EC-GEN-11) | retry |
| advice / call-to-action language (EC-GEN-12, 13) | advisory refusal |
| return or performance figures (EC-VAL-04) | performance reply |

The orchestrator retries once with the errors as feedback, then uses the fixed fallback.

`check_response` is the gate every reply passes, templates included: link rules per type
(EC-VAL-07, 08), footer, sentence count, no URLs in the text.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal
from urllib.parse import urlparse

from growwbuddy.constants import ALLOWED_DOMAIN
from growwbuddy.llm.schemas import MAX_SENTENCES, Answer
from growwbuddy.pipeline.types import NO_LINK_TYPES, Response
from growwbuddy.retrieval.headers import RetrievalHeaders, mentioned_schemes
from growwbuddy.retrieval.retriever import Retrieval

Action = Literal["ok", "retry", "advice", "performance"]

# ---------------------------------------------------------------- sentences

_ABBREVIATIONS = frozenset(
    {
        "rs", "e.g", "i.e", "ltd", "no", "nos", "vs", "etc", "mr", "ms", "mrs", "dr",
        "approx", "inc", "co", "pvt", "p.a", "st", "jr", "sr", "fig", "min", "max", "yr",
        "yrs", "mo", "est", "dept", "govt",
    }
)  # fmt: skip
_BULLET = re.compile(r"^\s*(?:[-*•▪◦]|\d{1,2}[.)])\s+")
_SENTENCE_END = re.compile(r"(?<=[.!?])[\"')\]]*\s+(?=\S)")


def _ends_with_abbreviation(piece: str) -> bool:
    words = piece.split()
    if not words:
        return False
    last = words[-1].lower().rstrip(".")
    return last in _ABBREVIATIONS or (len(last) == 1 and last.isalpha())


def split_sentences(text: str) -> list[str]:
    """Split into sentences. "Rs. 500", "e.g.", "Ltd." and "0.74%" don't end one; each line
    or list item is at least one sentence (EC-VAL-01, 02, 03)."""
    out: list[str] = []
    for raw_line in text.splitlines():
        line = _BULLET.sub("", raw_line).strip()
        if not line:
            continue
        pieces: list[str] = []
        start = 0
        for m in _SENTENCE_END.finditer(line):
            pieces.append(line[start : m.start()].strip())
            start = m.end()
        pieces.append(line[start:].strip())
        merged: list[str] = []
        for p in pieces:
            if merged and _ends_with_abbreviation(merged[-1]):
                merged[-1] = f"{merged[-1]} {p}"
            else:
                merged.append(p)
        out.extend(p for p in merged if p)
    return out


def count_sentences(parts: Sequence[str]) -> int:
    return sum(len(split_sentences(p)) for p in parts)


# ---------------------------------------------------------------- patterns

_URL = re.compile(
    r"https?://|\bwww\.|\b[a-z0-9-]+\.(?:in|com|org|net|co|io|gov|edu|info|app)\b(?:/|\b)",
    re.IGNORECASE,
)

_ADVICE = re.compile(
    r"\b(?:we|i) (?:would )?(?:recommend|suggest|advise)\b|\brecommended\b|"
    r"\byou (?:should|must|may want to|might want to|could consider)\b|"
    r"\bconsider (?:investing|buying|adding)\b|\bshould (?:invest|buy|sell|consider)\b|"
    r"\b(?:good|great|smart|wise|excellent|ideal|right) (?:choice|option|pick|fund|investment|"
    r"time)\b|\b(?:better|worse|cheaper|safer|riskier) than\b|\b(?:out|under)perform\w*\b|"
    r"\battractive\b|\bworth (?:it|investing|buying|considering)\b|\bideal for\b|"
    r"\bsuitable for\b|\bsafe (?:bet|investment|choice)\b|\bbeat(?:s|ing)? the\b|"
    r"\b(?:click here|invest now|buy now|start investing|start (?:a|your) sip (?:now|today)|"
    r"don'?t miss|act now|sign up)\b",
    re.IGNORECASE,
)

_PERCENT = re.compile(r"\d+(?:\.\d+)?\s*(?:%|per ?cent\b)", re.IGNORECASE)
_RETURN_WORDS = re.compile(
    # Not plain "growth": every plan here is "Direct Growth".
    r"\b(?:returns?|returned|cagr|xirr|gain(?:s|ed)?|grew|grows|grown|growth (?:of|was|rate)|"
    r"yield(?:s|ed)?|profits?|rose|risen|fell|jump(?:ed)?|perform(?:ed|ance)?|annuali[sz]ed)\b",
    re.IGNORECASE,
)
# Tax text says "returns are taxed at 20%": that's a tax rate, not a return figure
# (corpus-notes quirk 4).
_TAX = re.compile(r"\btax(?:ed|es|ation|able)?\b|\bltcg\b|\bstcg\b|\bslab\b", re.IGNORECASE)
_NAV_FIGURE = re.compile(r"\bnav\b[^.]{0,25}\d|\bnet asset value\b[^.]{0,25}\d", re.IGNORECASE)

# ---------------------------------------------------------------- number grounding (EC-GEN-10)

Unit = Literal["percent", "rupee", "plain"]
Quantity = tuple[Unit, Decimal]

# A number with the unit around it: "1.00%", "₹100", "Rs. 500", "100 rupees", "1.25 lakh".
# Commas are thousands separators in Indian grouping ("1,25,000"), never decimal points.
_QUANTITY = re.compile(
    r"(?P<rupee_pre>(?:₹|\brs\b\.?|\binr\b)\s*)?"
    r"(?<![\d.,])(?P<num>\d+(?:,\d+)*(?:\.\d+)?)(?![\d])"
    r"(?:\s*(?P<pct>%|per ?cent\b)|\s*(?P<scale>lakhs?|lacs?|crores?)\b|"
    r"\s*(?P<rupee_post>rupees?\b|/-))?",
    re.IGNORECASE,
)
_SCALE = {"lakh": Decimal(100_000), "lac": Decimal(100_000), "crore": Decimal(10_000_000)}


def quantities_in(text: str) -> set[Quantity]:
    """Every number in `text` as (unit, value). Values compare numerically, so "1%",
    "1.0%" and "1.00%" are the same quantity, and "01 Jan" equals "1 Jan"."""
    out: set[Quantity] = set()
    for m in _QUANTITY.finditer(text):
        value = Decimal(m["num"].replace(",", ""))
        unit: Unit = "plain"
        if m["pct"]:
            unit = "percent"
        elif m["scale"]:
            # "lakh"/"crore" amounts are rupees here; scale so "Rs 1.25 lakh" == "₹1,25,000".
            value *= _SCALE[m["scale"].lower().rstrip("s")]
            unit = "rupee"
        elif m["rupee_pre"] or m["rupee_post"]:
            unit = "rupee"
        out.add((unit, value.normalize()))
    return out


def ungrounded_quantities(answer: str, source: str) -> list[Quantity]:
    """Quantities in `answer` that `source` doesn't state.

    A number with a unit must match a number of the same unit and value in the source, so
    "1%" never matches "₹1". A bare number ("1 year", "10 Nov 2026") may match a source
    number of any unit with the same value: the model can drop a unit word, but it can't
    change a value.
    """
    src = quantities_in(source)
    any_unit = {v for _, v in src}
    return sorted(
        (
            q
            for q in quantities_in(answer)
            if (q[1] not in any_unit if q[0] == "plain" else q not in src)
        ),
        key=lambda q: (q[0], q[1]),
    )


def has_return_figure(text: str) -> bool:
    if _NAV_FIGURE.search(text):
        return True
    for sentence in split_sentences(text) or [text]:
        if (
            _PERCENT.search(sentence)
            and _RETURN_WORDS.search(sentence)
            and not _TAX.search(sentence)
        ):
            return True
    return False


def has_advice(text: str) -> bool:
    return bool(_ADVICE.search(text))


def has_url(text: str) -> bool:
    return bool(_URL.search(text))


def allowed_url(url: str) -> bool:
    p = urlparse(url)
    host = (p.hostname or "").lower()
    return p.scheme == "https" and (host == ALLOWED_DOMAIN or host.endswith("." + ALLOWED_DOMAIN))


# ---------------------------------------------------------------- answer check


@dataclass(frozen=True)
class AnswerCheck:
    action: Action
    errors: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.action == "ok"


def validate_answer(
    answer: Answer,
    retrieval: Retrieval,
    scheme: str | None,
    headers: RetrievalHeaders | None = None,
) -> AnswerCheck:
    text = " ".join(answer.sentences)

    # Compliance first: these replace the answer instead of retrying it.
    if has_advice(text):
        return AnswerCheck("advice", ("the answer contains advice or a call to action",))
    if has_return_figure(text):
        return AnswerCheck("performance", ("the answer states a return or performance figure",))

    errors: list[str] = []
    n = count_sentences(answer.sentences)
    if n < 1 or n > MAX_SENTENCES:
        errors.append(f"the answer has {n} sentences; write 1 to {MAX_SENTENCES}")
    if has_url(text):
        errors.append("the answer contains a URL or website name; remove it")

    cited = retrieval.get(answer.source_chunk_id) if answer.source_chunk_id else None
    if answer.source_chunk_id is None:
        errors.append("no source_chunk_id; cite exactly one context item ID")
    elif cited is None:
        errors.append("source_chunk_id is not one of the context item IDs; use one exactly")
    else:
        if not allowed_url(cited.source_url):
            errors.append("the cited item's source is not an allowed domain")
        if scheme is not None and cited.scheme != scheme:
            errors.append("the cited item is about a different scheme than the question")
        if ungrounded_quantities(text, cited.text):
            errors.append(
                "the answer has numbers that the cited item doesn't state; "
                "quote numbers exactly as the context gives them"
            )
        others = (
            set(mentioned_schemes(text, headers))
            - {cited.scheme}
            - set(mentioned_schemes(cited.text, headers))
        )
        if others:
            errors.append("the answer names a scheme other than the one asked about")

    return AnswerCheck("retry", tuple(errors)) if errors else AnswerCheck("ok")


# ---------------------------------------------------------------- every response

# Template replies that may come without a link when no scheme is known (task 1.2,
# EC-VAL-07): the link would otherwise have to be an extra, non-corpus URL.
LINK_OPTIONAL_WITHOUT_SCHEME = frozenset({"refusal", "performance", "not_found"})


def check_response(resp: Response) -> list[str]:
    """Problems with a finished reply; empty when it may be sent."""
    problems: list[str] = []
    if resp.type in NO_LINK_TYPES:
        if resp.citation is not None:
            problems.append(f"{resp.type} must not have a link")
        if resp.footer is not None:
            problems.append(f"{resp.type} must not have a footer")
    elif resp.citation is None:
        if not (resp.type in LINK_OPTIONAL_WITHOUT_SCHEME and resp.scheme is None):
            problems.append(f"{resp.type} needs exactly one link")
    else:
        if not allowed_url(resp.citation.url):
            problems.append("link is not on the allowed domain")
        if not resp.footer:
            problems.append("a reply with a link needs the footer")
    if not resp.text.strip():
        problems.append("empty text")
    if has_url(resp.text):
        problems.append("URL in the reply text")
    if resp.type == "answer":
        n = len(split_sentences(resp.text))
        if not 1 <= n <= MAX_SENTENCES:
            problems.append(f"answer has {n} sentences")
        if has_advice(resp.text) or has_return_figure(resp.text):
            problems.append("answer contains advice or a return figure")
    return problems
