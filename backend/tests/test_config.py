"""EV-0-03, EV-0-04: config defaults and lazy API key."""

from __future__ import annotations

import pytest

from growwbuddy.config import DATA_DIR, MissingApiKeyError, Settings

ENV_VARS = [
    "GROQ_API_KEY",
    "GROWWBUDDY_MODEL",
    "GROWWBUDDY_REASONING_EFFORT",
    "GROWWBUDDY_LLM_TIMEOUT_S",
    "GROWWBUDDY_LLM_MAX_RETRIES",
    "GROWWBUDDY_DB_URL",
    "GROWWBUDDY_EMBED_MODEL",
    "GROWWBUDDY_HISTORY_TURNS",
    "GROWWBUDDY_RETRIEVAL_MIN_SCORE",
    "GROWWBUDDY_STALE_DAYS",
    "GROWWBUDDY_FRONTEND_ORIGIN",
    "GROWWBUDDY_CORPUS_DIR",
    "GROWWBUDDY_PARSED_DIR",
    "GROWWBUDDY_RAW_KEEP",
]


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def make_settings() -> Settings:
    # _env_file=None: ignore any developer .env so the test only sees defaults.
    return Settings(_env_file=None)


@pytest.mark.usefixtures("clean_env")
def test_EV_0_03_defaults() -> None:
    s = make_settings()
    assert s.model == "openai/gpt-oss-120b"
    assert s.reasoning_effort == "low"
    assert s.llm_timeout_s == 30.0
    assert s.llm_max_retries == 2
    assert s.db_url.endswith("/data/growwbuddy.db")
    assert s.db_url.startswith("sqlite:///")
    assert s.embed_model == "intfloat/e5-small-v2"
    assert not hasattr(s, "chroma_dir")
    assert s.history_turns == 6
    assert s.retrieval_min_score is None
    assert s.stale_days == 45
    assert s.frontend_origin == "http://localhost:5173"
    assert s.groq_api_key is None
    assert s.corpus_dir.name == "corpus"
    assert s.parsed_dir == DATA_DIR / "parsed"
    assert s.raw_keep == 5


@pytest.mark.usefixtures("clean_env")
def test_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GROWWBUDDY_MODEL", "some/other-model")
    monkeypatch.setenv("GROWWBUDDY_REASONING_EFFORT", "medium")
    monkeypatch.setenv("GROWWBUDDY_RETRIEVAL_MIN_SCORE", "0.42")
    s = make_settings()
    assert s.model == "some/other-model"
    assert s.reasoning_effort == "medium"
    assert s.retrieval_min_score == 0.42


@pytest.mark.usefixtures("clean_env")
def test_invalid_reasoning_effort_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GROWWBUDDY_REASONING_EFFORT", "extreme")
    with pytest.raises(ValueError):
        make_settings()


@pytest.mark.usefixtures("clean_env")
def test_EV_0_04_api_key_optional_until_used() -> None:
    s = make_settings()  # constructing settings must not need a key
    with pytest.raises(MissingApiKeyError, match="GROQ_API_KEY"):
        s.require_groq_api_key()


@pytest.mark.usefixtures("clean_env")
def test_api_key_read_and_hidden(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test_value")
    s = make_settings()
    assert s.require_groq_api_key() == "gsk_test_value"
    assert "gsk_test_value" not in repr(s)
