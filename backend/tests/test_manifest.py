"""EV-1-01 / EC-ING-20: the manifest is exactly the five fixed Groww URLs."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest
import yaml

from growwbuddy.constants import SOURCES
from growwbuddy.ingest.manifest import (
    DEFAULT_MANIFEST_PATH,
    ManifestError,
    is_allowed_url,
    load_manifest,
    parse_manifest,
)


def real_data() -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load(DEFAULT_MANIFEST_PATH.read_text(encoding="utf-8"))
    return copy.deepcopy(data)


def test_real_manifest_loads() -> None:
    m = load_manifest()
    assert {(s.id, s.scheme, s.url) for s in m.sources} == set(SOURCES)
    assert m.allowed_domains == ("groww.in",)


def test_EV_1_01_rejects_sixth_url() -> None:
    data = real_data()
    data["sources"].append(
        {
            "id": "groww-hdfc-flexi-cap",
            "scheme": "hdfc-mid-cap",
            "url": "https://groww.in/mutual-funds/hdfc-flexi-cap-fund-direct-growth",
            "format": "html",
            "doc_type": "scheme_page",
        }
    )
    with pytest.raises(ManifestError):
        parse_manifest(data)


def test_EV_1_01_rejects_removed_entry() -> None:
    data = real_data()
    data["sources"].pop()
    with pytest.raises(ManifestError, match="missing"):
        parse_manifest(data)


def test_EV_1_01_rejects_other_domain() -> None:
    data = real_data()
    data["sources"][0]["url"] = "https://example.com/mutual-funds/hdfc-mid-cap-fund-direct-growth"
    with pytest.raises(ManifestError, match="allowed domain"):
        parse_manifest(data)


def test_EV_1_01_rejects_changed_url() -> None:
    data = real_data()
    data["sources"][0]["url"] = "https://groww.in/mutual-funds/hdfc-mid-cap-fund-regular-growth"
    with pytest.raises(ManifestError, match="differ"):
        parse_manifest(data)


def test_EV_1_01_rejects_duplicate_id() -> None:
    data = real_data()
    data["sources"][1]["id"] = data["sources"][0]["id"]
    with pytest.raises(ManifestError, match="duplicate source id"):
        parse_manifest(data)


def test_rejects_extra_allowed_domain() -> None:
    data = real_data()
    data["allowed_domains"].append("amfiindia.com")
    with pytest.raises(ManifestError, match="allowed_domains"):
        parse_manifest(data)


def test_rejects_unknown_field() -> None:
    data = real_data()
    data["sources"][0]["extra"] = "x"
    with pytest.raises(ManifestError):
        parse_manifest(data)


def test_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ManifestError, match="not found"):
        load_manifest(tmp_path / "nope.yaml")


@pytest.mark.parametrize(
    ("url", "ok"),
    [
        ("https://groww.in/mutual-funds/x", True),
        ("https://www.groww.in/x", True),
        ("http://groww.in/x", False),  # https only
        ("https://evilgroww.in/x", False),
        ("https://groww.in.evil.com/x", False),
        ("https://example.com/?u=groww.in", False),
    ],
)
def test_is_allowed_url(url: str, ok: bool) -> None:
    assert is_allowed_url(url) is ok
