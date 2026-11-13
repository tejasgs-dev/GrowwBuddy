"""Retrieval headers: scheme aliases and section keywords (task 2.4).

The data lives in `corpus/retrieval_headers.yaml` so it can be tuned without code changes.
The scheme aliases are also what the Phase 3 router uses to resolve scheme names
(EC-INT-21), so `resolve_scheme` lives here too.
"""

from __future__ import annotations

import re
from functools import cache
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, model_validator

from growwbuddy.config import settings
from growwbuddy.constants import SCHEME_KEYS, SCHEME_NAMES


class HeadersError(ValueError):
    """retrieval_headers.yaml is missing or malformed."""


class SchemeHeader(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    aliases: tuple[str, ...]


class SectionHeader(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    keywords: tuple[str, ...]


class RetrievalHeaders(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schemes: dict[str, SchemeHeader]
    sections: dict[str, SectionHeader]

    @model_validator(mode="after")
    def _check(self) -> RetrievalHeaders:
        if set(self.schemes) != set(SCHEME_KEYS):
            raise ValueError(f"schemes must be exactly {sorted(SCHEME_KEYS)}")
        seen: dict[str, str] = {}
        for key, s in self.schemes.items():
            for alias in s.aliases:
                norm = normalise(alias)
                if norm in seen and seen[norm] != key:
                    raise ValueError(f"alias '{alias}' used for both {seen[norm]} and {key}")
                seen[norm] = key
        return self

    def header(self, scheme: str, section: str, title: str) -> str:
        """`HDFC Mid Cap Fund (mid cap, midcap) | Expense ratio | TER | ...`"""
        aliases = self.schemes[scheme].aliases
        name = SCHEME_NAMES[scheme] + (f" ({', '.join(aliases)})" if aliases else "")
        sec = self.sections.get(section)
        keywords = sec.keywords if sec else ()
        return " | ".join([name, title, *keywords])


def default_path() -> Path:
    return settings.corpus_dir / "retrieval_headers.yaml"


def load_headers(path: Path | None = None) -> RetrievalHeaders:
    path = path or default_path()
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        return RetrievalHeaders.model_validate(data)
    except FileNotFoundError as e:
        raise HeadersError(f"retrieval headers not found: {path}") from e
    except (yaml.YAMLError, ValueError) as e:
        raise HeadersError(f"invalid retrieval headers in {path}: {e}") from e


@cache
def headers() -> RetrievalHeaders:
    return load_headers()


def normalise(text: str) -> str:
    """Lower-case, hyphens and punctuation to spaces, single spaces."""
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text.lower()).split())


def mentioned_schemes(text: str, h: RetrievalHeaders | None = None) -> list[str]:
    """Every in-scope scheme `text` names, in the order they first appear."""
    h = h or headers()
    padded = f" {normalise(text)} "
    first: dict[str, int] = {}
    for key, s in h.schemes.items():
        for name in (*s.aliases, SCHEME_NAMES[key]):
            pos = padded.find(f" {normalise(name)} ")
            if pos >= 0 and (key not in first or pos < first[key]):
                first[key] = pos
    return sorted(first, key=first.__getitem__)


def resolve_scheme(text: str, h: RetrievalHeaders | None = None) -> str | None:
    """Return the one scheme `text` names, or None if it names none or several.

    Simple alias matching for the debug tool and as a router helper. The Phase 3 router
    adds an LLM fallback for typos and indirect references.
    """
    found = mentioned_schemes(text, h)
    return found[0] if len(found) == 1 else None
