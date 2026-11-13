"""Phase 5 backend pieces: the built UI is served at / (task 5.10, EV-5-14) and the UI's
PII patterns match the server guard (task 5.8, EV-5-07)."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from growwbuddy.config import Settings
from growwbuddy.main import create_app
from growwbuddy.pipeline import pii_export
from tests.api_helpers import CLIENT_A, ORIGIN, make_services

INDEX_HTML = "<!doctype html><title>GrowwBuddy</title><div id=root></div>"


def _app(sf: sessionmaker[Session], dist: Path) -> TestClient:
    s = Settings(_env_file=None, frontend_origin=ORIGIN, frontend_dist=dist)
    return TestClient(create_app(make_services(sf), s=s))


def test_built_frontend_is_served_at_root(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    (tmp_path / "index.html").write_text(INDEX_HTML, encoding="utf-8")
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "app.js").write_text("console.log(1)", encoding="utf-8")
    client = _app(session_factory, tmp_path)

    root = client.get("/")
    assert root.status_code == 200
    assert "GrowwBuddy" in root.text
    assert root.headers["content-type"].startswith("text/html")
    assert client.get("/assets/app.js").status_code == 200

    # The API and health routes still win over the static mount.
    assert client.get("/healthz").json()["status"] == "ok"
    assert client.get("/api/meta").status_code == 200
    r = client.get("/api/threads", params={"client_id": CLIENT_A})
    assert r.status_code == 200 and r.json() == {"threads": []}
    assert client.get("/api/threads/thr_nope/messages").status_code in {404, 422}


def test_no_frontend_build_means_api_only(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    client = _app(session_factory, tmp_path / "missing")
    assert client.get("/").status_code == 404
    assert client.get("/healthz").status_code == 200


def test_frontend_cannot_read_files_outside_dist(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text(INDEX_HTML, encoding="utf-8")
    (tmp_path / "secret.env").write_text("GROQ_API_KEY=x", encoding="utf-8")
    client = _app(session_factory, dist)
    for path in ("/../secret.env", "/%2e%2e/secret.env", "/..%2fsecret.env"):
        r = client.get(path)
        assert "GROQ_API_KEY" not in r.text


# ---------------------------------------------------------------- shared PII patterns


def test_pii_patterns_file_is_in_sync() -> None:
    """EV-5-07: the committed JSON equals what the backend guard exports."""
    assert pii_export.OUTPUT_PATH.exists(), "run `make pii-patterns`"
    assert pii_export.OUTPUT_PATH.read_text(encoding="utf-8") == pii_export.export()


def test_exported_patterns_match_guard_behaviour() -> None:
    data = json.loads(pii_export.export())
    pats = {
        k: re.compile(v["source"], re.IGNORECASE if "i" in v["flags"] else 0)
        for k, v in data["patterns"].items()
    }
    assert pats["pan"].search("my pan is abcde1234f")
    assert pats["phone"].search("call 9876543210")
    assert pats["email"].search("me@example.com")
    assert not pats["pan"].search("HDFC Mid Cap Fund")
    assert data["near"] == 30


def test_pii_export_check_mode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pii_export, "OUTPUT_PATH", tmp_path / "pii.json")
    assert pii_export.main(["--check"]) == 1
    assert pii_export.main([]) == 0
    assert pii_export.main(["--check"]) == 0
