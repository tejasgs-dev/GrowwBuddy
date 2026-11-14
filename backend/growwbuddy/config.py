"""Application settings, read from environment variables and the project-root `.env` file.

See "LLM Provider: Groq" > "Configuration" in docs/implementation-plan.md for the full list.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

# backend/growwbuddy/config.py -> project root is two levels above the package.
PROJECT_ROOT: Path = Path(__file__).resolve().parents[2]
DATA_DIR: Path = PROJECT_ROOT / "data"

ReasoningEffort = Literal["low", "medium", "high"]


class MissingApiKeyError(RuntimeError):
    """Raised when a model call is attempted without GROQ_API_KEY set."""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="GROWWBUDDY_",
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    # --- LLM (Groq) ---
    groq_api_key: SecretStr | None = Field(default=None, validation_alias="GROQ_API_KEY")
    # GROQ_MODEL; the older GROWWBUDDY_MODEL is still accepted so existing .env files work.
    model: str = Field(
        default="openai/gpt-oss-120b",
        validation_alias=AliasChoices("GROQ_MODEL", "GROWWBUDDY_MODEL"),
    )
    reasoning_effort: ReasoningEffort = "low"
    llm_timeout_s: float = Field(default=30.0, gt=0)
    llm_max_retries: int = Field(default=2, ge=0)
    # Longest retry-after a chat request waits on a 429; longer gets the busy reply.
    llm_max_retry_wait_s: float = Field(default=10.0, ge=0)

    # --- Groq rate limits for the organisation (implementation plan, "Groq rate limits") ---
    groq_rpm: int = Field(default=30, gt=0)
    groq_rpd: int = Field(default=1000, gt=0)
    groq_tpm: int = Field(default=8000, gt=0)
    groq_tpd: int = Field(default=200_000, gt=0)
    groq_headroom: float = Field(default=0.8, gt=0, le=1)

    # --- Per-client and per-IP limits on model-answered messages (task 6.4) ---
    # Rule-handled messages (PII, advisory, NAV, greeting) don't count: they cost no tokens.
    client_per_minute: int = Field(default=2, ge=1)
    client_per_day: int = Field(default=20, ge=1)
    # A little higher than per client, for shared networks (EC-API-05).
    ip_per_minute: int = Field(default=4, ge=1)
    ip_per_day: int = Field(default=40, ge=1)

    # --- Storage ---
    db_url: str = f"sqlite:///{(DATA_DIR / 'growwbuddy.db').as_posix()}"
    corpus_dir: Path = PROJECT_ROOT / "corpus"  # sources.yaml and raw/ downloads
    parsed_dir: Path = DATA_DIR / "parsed"  # parsed page JSON, one file per source
    raw_keep: int = Field(default=5, ge=1)  # raw HTML copies kept per source (EC-OPS-04)

    # --- Retrieval ---
    # Vectors live in the db_url database; there is no separate vector store.
    embed_model: str = "intfloat/e5-small-v2"
    # How the embedding model runs. "torch": sentence-transformers (ingest, CI, the daily
    # refresh). "onnx": ONNX Runtime with the repo's model.onnx; same vectors, about half the
    # memory, no torch import (the Render free tier, docs/deployment-plan.md).
    embed_runtime: Literal["torch", "onnx"] = "torch"
    # ONNX Runtime threads; 0 = one per core. Set 1 on a fractional-CPU host.
    embed_threads: int = Field(default=0, ge=0)
    history_turns: int = Field(default=6, ge=0)
    # Low-confidence cutoff (task 6.3): the top score, and the gap between the top two
    # scores. None switches that check off. See docs/implementation-plan.md, Phase 6 results.
    retrieval_min_score: float | None = None
    retrieval_min_gap: float | None = None

    # --- Freshness ---
    # Days since the last successful fetch (or daily-refresh heartbeat) before answers add
    # "(source may be out of date)". 3 = two missed daily runs (task 7.7).
    stale_days: int = Field(default=3, gt=0)

    # --- Daily index bundles (Phase 7, task 7.6) ---
    # GitHub repository ("owner/name") whose `index-*` releases the app pulls. None: no
    # polling (local `make ingest` only).
    index_repo: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
    index_poll_minutes: int = Field(default=60, ge=1)
    # Read-only token, only needed when the repository is private.
    index_token: SecretStr | None = None

    # --- Retention (task 6.7) ---
    # Threads with no activity for this many days are deleted; 0 keeps them forever.
    thread_retention_days: int = Field(default=30, ge=0)

    # --- API ---
    frontend_origin: str = "http://localhost:5173"
    # Built web UI (task 5.10). Served at `/` when its index.html exists; `npm run build`.
    frontend_dist: Path = PROJECT_ROOT / "frontend" / "dist"

    def require_groq_api_key(self) -> str:
        """Return the Groq API key, or fail clearly. Call only right before a model call."""
        if self.groq_api_key is None or not self.groq_api_key.get_secret_value().strip():
            raise MissingApiKeyError(
                "GROQ_API_KEY is not set. Add it to the .env file in the project root "
                "(see .env.example). It is only needed for live model calls."
            )
        return self.groq_api_key.get_secret_value()


settings = Settings()
