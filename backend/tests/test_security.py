"""Security checks (task 6.10): EV-6-11 no secrets or config names in the frontend.

The built bundle is checked when it exists (CI's frontend job also greps it); the source
is always checked, since that's what the bundle is built from.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from growwbuddy.config import PROJECT_ROOT, Settings

FRONTEND = PROJECT_ROOT / "frontend"
KEY_LIKE = re.compile(r"gsk_[A-Za-z0-9]{20,}")


def config_names() -> set[str]:
    """Every environment variable name the backend reads."""
    names = {"GROQ_API_KEY", "GROQ_MODEL", "GROWWBUDDY_MODEL"}
    for name, field in Settings.model_fields.items():
        if field.validation_alias is None:
            names.add(f"GROWWBUDDY_{name.upper()}")
    return names


def _scan(files: list[Path]) -> list[str]:
    names = config_names()
    hits = []
    for f in files:
        text = f.read_text(encoding="utf-8", errors="ignore")
        if KEY_LIKE.search(text):
            hits.append(f"{f}: key-like string")
        hits += [f"{f}: {n}" for n in names if n in text]
    return hits


def test_config_names_cover_the_settings() -> None:
    names = config_names()
    assert "GROWWBUDDY_DB_URL" in names and "GROWWBUDDY_CLIENT_PER_DAY" in names
    assert "GROQ_API_KEY" in names


def test_EV_6_11_frontend_source_has_no_secrets_or_config_names() -> None:
    src = FRONTEND / "src"
    files = [
        p for p in src.rglob("*") if p.is_file() and p.suffix in {".ts", ".tsx", ".json", ".css"}
    ]
    assert files
    assert _scan(files) == []


def test_EV_6_11_built_bundle_has_no_secrets_or_config_names() -> None:
    dist = FRONTEND / "dist"
    if not (dist / "index.html").is_file():
        pytest.skip("frontend not built (make frontend-build); CI's frontend job checks it")
    files = [p for p in dist.rglob("*") if p.is_file() and p.suffix in {".js", ".html", ".css"}]
    assert _scan(files) == []
