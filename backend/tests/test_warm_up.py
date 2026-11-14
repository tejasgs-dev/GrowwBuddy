"""The embedding model loads at startup, before the server reports ready.

No real model: a stand-in embedder with the same `loaded` / `warm_up` surface as
`E5Embedder`.
"""

from __future__ import annotations

from collections.abc import Sequence

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from growwbuddy.config import Settings
from growwbuddy.main import create_app
from growwbuddy.retrieval.embed import E5Embedder, EmbedderUnavailableError, Vectors
from growwbuddy.retrieval.retriever import Retriever
from growwbuddy.retrieval.vector import VectorIndex
from tests.api_helpers import make_services
from tests.fakes import FakeEmbedder


class LazyEmbedder(FakeEmbedder):
    """Like E5Embedder: not loaded until `warm_up` or the first query."""

    def __init__(self, fail: bool = False) -> None:
        super().__init__()
        self.loaded = False
        self.fail = fail
        self.warm_ups = 0

    def warm_up(self) -> None:
        self.warm_ups += 1
        if self.fail:
            raise EmbedderUnavailableError("no model here")
        self.loaded = True

    def embed_query(self, text: str) -> Vectors:
        self.loaded = True
        return super().embed_query(text)

    def embed_passages(self, texts: Sequence[str]) -> Vectors:
        self.loaded = True
        return super().embed_passages(texts)


def retriever(sf: sessionmaker[Session], emb: FakeEmbedder) -> Retriever:
    return Retriever(VectorIndex(sf, emb.model_name), emb, sf)


def test_e5_embedder_starts_unloaded() -> None:
    assert E5Embedder().loaded is False  # constructing it doesn't import torch


def test_ready_needs_the_embedding_model_loaded(indexed: sessionmaker[Session]) -> None:
    emb = LazyEmbedder()
    r = retriever(indexed, emb)
    assert not r.ready()  # index is fine, model not loaded yet
    r.warm_up()
    assert r.ready() and emb.warm_ups == 1


def test_embedder_without_loaded_flag_counts_as_loaded(indexed: sessionmaker[Session]) -> None:
    r = retriever(indexed, FakeEmbedder())
    r.warm_up()  # no warm_up method: a no-op
    assert r.ready()


def test_server_loads_the_model_before_it_reports_ready(indexed: sessionmaker[Session]) -> None:
    emb = LazyEmbedder()
    r = retriever(indexed, emb)
    svc = make_services(indexed, ready=r.ready)
    svc.warm_up = r.warm_up
    assert not r.ready()
    with TestClient(create_app(svc, s=Settings(_env_file=None))) as client:
        assert emb.warm_ups == 1  # ran during startup, before any request
        assert client.get("/healthz").json() == {"status": "ok", "ready": True}


def test_failed_model_load_keeps_the_server_up_not_ready(
    indexed: sessionmaker[Session], caplog: pytest.LogCaptureFixture
) -> None:
    emb = LazyEmbedder(fail=True)
    r = retriever(indexed, emb)
    svc = make_services(indexed, ready=r.ready)
    svc.warm_up = r.warm_up
    with TestClient(create_app(svc, s=Settings(_env_file=None))) as client:
        assert client.get("/healthz").json() == {"status": "ok", "ready": False}
    assert any("embedding model failed to load" in rec.getMessage() for rec in caplog.records)
