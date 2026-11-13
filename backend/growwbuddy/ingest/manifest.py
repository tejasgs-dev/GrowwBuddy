"""Load and validate corpus/sources.yaml (task 1.3).

The manifest must contain exactly the five fixed Groww URLs from the implementation plan.
Any added, removed, changed or duplicated entry is a hard error.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

from growwbuddy.config import PROJECT_ROOT
from growwbuddy.constants import ALLOWED_DOMAIN, SCHEME_KEYS, SOURCES

DEFAULT_MANIFEST_PATH = PROJECT_ROOT / "corpus" / "sources.yaml"


class ManifestError(ValueError):
    """The manifest is missing, malformed, or differs from the fixed source list."""


def is_allowed_url(url: str, allowed_domains: Iterable[str] = (ALLOWED_DOMAIN,)) -> bool:
    """True if `url` is https and its host is an allowed domain or a subdomain of one."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or not host:
        return False
    return any(host == d or host.endswith("." + d) for d in allowed_domains)


class SourceEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    scheme: str
    url: str
    format: Literal["html"]
    doc_type: Literal["scheme_page"]

    @property
    def slug(self) -> str:
        """Last path segment of the URL, e.g. 'hdfc-mid-cap-fund-direct-growth'."""
        return urlsplit(self.url).path.rstrip("/").rsplit("/", 1)[-1]


class Manifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    allowed_domains: tuple[str, ...]
    sources: tuple[SourceEntry, ...]

    @model_validator(mode="after")
    def _check_fixed_list(self) -> Manifest:
        if self.allowed_domains != (ALLOWED_DOMAIN,):
            raise ValueError(f"allowed_domains must be exactly [{ALLOWED_DOMAIN}]")

        ids = [s.id for s in self.sources]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate source id")
        schemes = [s.scheme for s in self.sources]
        if len(schemes) != len(set(schemes)):
            raise ValueError("duplicate scheme key")

        for s in self.sources:
            if s.scheme not in SCHEME_KEYS:
                raise ValueError(f"unknown scheme key: {s.scheme}")
            if not is_allowed_url(s.url, self.allowed_domains):
                raise ValueError(f"URL not on an allowed domain: {s.url}")

        expected = set(SOURCES)
        actual = {(s.id, s.scheme, s.url) for s in self.sources}
        if actual != expected:
            extra = sorted(actual - expected)
            missing = sorted(expected - actual)
            raise ValueError(
                f"sources differ from the fixed list; unexpected={extra} missing={missing}"
            )
        return self

    def get(self, source_id: str) -> SourceEntry:
        for s in self.sources:
            if s.id == source_id:
                return s
        raise KeyError(source_id)


def parse_manifest(data: object) -> Manifest:
    try:
        return Manifest.model_validate(data)
    except ValidationError as e:
        raise ManifestError(f"invalid corpus manifest: {e}") from e


def load_manifest(path: Path = DEFAULT_MANIFEST_PATH) -> Manifest:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as e:
        raise ManifestError(f"corpus manifest not found: {path}") from e
    except yaml.YAMLError as e:
        raise ManifestError(f"corpus manifest is not valid YAML: {e}") from e
    return parse_manifest(data)
