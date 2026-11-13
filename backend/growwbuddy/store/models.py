"""SQLAlchemy models.

Phase 1: source registry. Phase 2: chunks, index meta, scheme facts. Phase 4: threads and
messages.
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class SourceState(Base):
    """One row per corpus source: fetch history and content hash (task 1.5).

    `sha256` hashes the extracted fund content, not the raw HTML, so ad or layout
    changes don't trigger re-parsing (EC-ING-09). `fetched_at` is the last *successful*
    fetch and drives staleness (§13).
    """

    __tablename__ = "sources_state"

    source_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    http_last_modified: Mapped[str | None] = mapped_column(String(64))
    sha256: Mapped[str | None] = mapped_column(String(64))
    # ok | unchanged | failed
    status: Mapped[str] = mapped_column(String(16), default="new")
    last_error: Mapped[str | None] = mapped_column(Text)


class ChunkRow(Base):
    """One searchable chunk per parsed section, with its embedding (tasks 2.1, 2.5).

    `text` is what the model and the user see. `embed_text` adds the retrieval header
    (scheme aliases + section keywords) and is only ever embedded.
    """

    __tablename__ = "chunks"

    chunk_id: Mapped[str] = mapped_column(String(128), primary_key=True)  # <source_id>#<section>
    source_id: Mapped[str] = mapped_column(String(64), index=True)
    scheme: Mapped[str] = mapped_column(String(64), index=True)
    section: Mapped[str] = mapped_column(String(64))
    title: Mapped[str] = mapped_column(String(128))
    doc_type: Mapped[str] = mapped_column(String(32))
    source_url: Mapped[str] = mapped_column(String(512))
    as_of_date: Mapped[date] = mapped_column(Date)  # the page's data date
    fact_as_on: Mapped[date | None] = mapped_column(Date)  # the section's own fact date
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    text: Mapped[str] = mapped_column(Text)
    embed_text: Mapped[str] = mapped_column(Text)
    content_sha256: Mapped[str] = mapped_column(String(64))  # of `text`
    embed_model: Mapped[str] = mapped_column(String(128))
    dim: Mapped[int] = mapped_column(Integer)
    embedding: Mapped[bytes] = mapped_column(LargeBinary)  # float32, little-endian, normalised


class IndexMeta(Base):
    """Single row describing the current index (EC-ING-25, EC-RET-06).

    `generation` goes up on every rebuild, so a running server can tell the index changed.
    """

    __tablename__ = "index_meta"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    embed_model: Mapped[str] = mapped_column(String(128))
    dim: Mapped[int] = mapped_column(Integer)
    generation: Mapped[int] = mapped_column(Integer, default=0)
    chunk_count: Mapped[int] = mapped_column(Integer, default=0)
    # Hash of everything the index is built from (parsed pages, headers, model); an
    # ingest with an unchanged key skips the rebuild.
    build_key: Mapped[str] = mapped_column(String(64))
    built_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class SchemeFact(Base):
    """A verified-or-not structured fact for one scheme (§5.3, tasks 2.6-2.10).

    Only rows with `verified = true` and a value are used in answers (EC-RET-05).

    `extracted_value` is what the extractor produced on the last ingest; `value_text` is what
    answers use. They differ only after a manual correction. On re-ingest the row is kept
    as-is (review included) while `extracted_value` is unchanged; otherwise the old row is
    written to history and replaced.
    """

    __tablename__ = "scheme_facts"

    scheme: Mapped[str] = mapped_column(String(64), primary_key=True)
    fact_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value_text: Mapped[str | None] = mapped_column(Text)
    extracted_value: Mapped[str | None] = mapped_column(Text)
    source_id: Mapped[str] = mapped_column(String(64))
    source_url: Mapped[str] = mapped_column(String(512))
    section: Mapped[str | None] = mapped_column(String(64))  # chunk section holding the evidence
    as_of_date: Mapped[date] = mapped_column(Date)  # fact's own date, else the page's
    extracted_by: Mapped[str] = mapped_column(String(16))  # rule | llm | manual
    verified: Mapped[bool] = mapped_column(Boolean, default=False)
    check_error: Mapped[str | None] = mapped_column(Text)  # why it isn't verified
    evidence: Mapped[str | None] = mapped_column(Text)  # source snippet for the review tool
    extracted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    reviewed_by: Mapped[str | None] = mapped_column(String(64))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SchemeFactHistory(Base):
    """Old and new values whenever a re-ingest changes a fact (§13 fact diffing)."""

    __tablename__ = "scheme_facts_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    scheme: Mapped[str] = mapped_column(String(64), index=True)
    fact_key: Mapped[str] = mapped_column(String(64))
    old_value: Mapped[str | None] = mapped_column(Text)
    new_value: Mapped[str | None] = mapped_column(Text)
    old_verified: Mapped[bool | None] = mapped_column(Boolean)
    new_verified: Mapped[bool | None] = mapped_column(Boolean)
    old_extracted_by: Mapped[str | None] = mapped_column(String(16))
    source_id: Mapped[str] = mapped_column(String(64))
    changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Thread(Base):
    """One chat thread (architecture §9.1, task 4.1).

    `client_id` is a random UUID from the browser, not a user identity: there are no accounts,
    since an account would mean collecting an email or phone number. `title` stays NULL
    (shown as "New chat") until the first factual question is answered (EC-THR-15).
    """

    __tablename__ = "threads"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)  # thr_<ULID>
    client_id: Mapped[str] = mapped_column(String(36), index=True)
    title: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class Message(Base):
    """One stored message. A user message and its reply are always written together.

    PII-rejected and busy messages are never stored (P6, task 3.4). `id` is a monotonic ULID,
    so ordering by `id` is send order. Reply-only columns are NULL on user rows.
    """

    __tablename__ = "messages"
    __table_args__ = (
        # Duplicate-send protection (EC-THR-07); SQLite allows many NULLs here.
        UniqueConstraint("thread_id", "client_message_id", name="uq_messages_client_msg"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)  # msg_<ULID>
    thread_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("threads.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[str] = mapped_column(String(16))  # user | assistant
    content: Mapped[str] = mapped_column(Text)
    response_type: Mapped[str | None] = mapped_column(String(16))
    reason: Mapped[str | None] = mapped_column(String(32))
    scheme: Mapped[str | None] = mapped_column(String(64))  # carried into follow-ups
    citation_url: Mapped[str | None] = mapped_column(String(512))
    citation_title: Mapped[str | None] = mapped_column(String(128))
    as_of_date: Mapped[date | None] = mapped_column(Date)  # the footer date
    client_message_id: Mapped[str | None] = mapped_column(String(36))  # user rows only
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
