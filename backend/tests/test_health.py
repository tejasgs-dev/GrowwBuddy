"""Smoke test: the app starts and answers the liveness probe."""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from growwbuddy.main import app
from tests.api_helpers import client_for, make_services


def test_healthz(session_factory: sessionmaker[Session]) -> None:
    response = client_for(make_services(session_factory)).get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "ready": True}


def test_module_app_imports_without_side_effects() -> None:
    # Services are built at startup, not at import; without startup it reports not ready.
    assert app.state.services is None
    assert TestClient(app).get("/healthz").json() == {"status": "ok", "ready": False}
