"""OnnxE5Embedder (GROWWBUDDY_EMBED_RUNTIME=onnx, docs/deployment-plan.md).

The unit tests use a fake tokenizer and session, so CI never downloads the model. The last
test compares the ONNX and torch vectors with the real model; it is skipped unless
GROWWBUDDY_RUN_MODEL_TESTS=1.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import numpy as np
import pytest

from growwbuddy.config import Settings
from growwbuddy.retrieval.embed import E5Embedder, OnnxE5Embedder, make_embedder

DIM = 4


@dataclass
class _Enc:
    ids: list[int]
    attention_mask: list[int]
    type_ids: list[int]


class FakeTokenizer:
    """One token per word, padded to the longest text in the batch with id 0."""

    def __init__(self) -> None:
        self.seen: list[str] = []

    def encode_batch(self, texts: list[str]) -> list[_Enc]:
        self.seen += texts
        words = [t.split() for t in texts]
        width = max(len(w) for w in words)
        out = []
        for w in words:
            ids = [len(x) for x in w] + [0] * (width - len(w))
            mask = [1] * len(w) + [0] * (width - len(w))
            out.append(_Enc(ids, mask, [0] * width))
        return out


@dataclass
class _Input:
    name: str


class FakeSession:
    """Hidden state for a real token is [id, 1, 0, 0]; padding gets a huge value, so any
    leak of padding into the mean shows up."""

    def __init__(self, inputs: tuple[str, ...] = ("input_ids", "attention_mask")) -> None:
        self._inputs = inputs
        self.feeds: list[dict[str, Any]] = []

    def get_inputs(self) -> list[_Input]:
        return [_Input(n) for n in self._inputs]

    def run(self, _outputs: Any, feeds: dict[str, Any]) -> list[Any]:
        self.feeds.append(feeds)
        ids, mask = feeds["input_ids"], feeds["attention_mask"]
        h = np.zeros((*ids.shape, DIM), dtype=np.float32)
        h[..., 0] = ids
        h[..., 1] = 1.0
        h[mask == 0] = 1e6
        return [h]


def _embedder(session: FakeSession | None = None) -> tuple[OnnxE5Embedder, FakeTokenizer]:
    e = OnnxE5Embedder("intfloat/e5-small-v2")
    tok = FakeTokenizer()
    e._tokenizer, e._session = tok, session or FakeSession()
    return e, tok


def test_mean_pool_ignores_padding_and_normalises() -> None:
    e, _ = _embedder()
    # "passage: ab abcd" -> ids [8, 2, 4]; "passage: x" -> [8, 1] padded to 3.
    v = e.embed_passages(["ab abcd", "x"])
    assert v.shape == (2, DIM)
    np.testing.assert_allclose(np.linalg.norm(v, axis=1), 1.0, rtol=1e-6)
    expected = np.array([[14 / 3, 1, 0, 0], [9 / 2, 1, 0, 0]], dtype=np.float32)
    expected /= np.linalg.norm(expected, axis=1, keepdims=True)
    np.testing.assert_allclose(v, expected, rtol=1e-6)


def test_prefixes_match_the_torch_embedder() -> None:
    e, tok = _embedder()
    e.embed_query("exit load")
    e.embed_passages(["text"])
    assert tok.seen == [E5Embedder.QUERY_PREFIX + "exit load", E5Embedder.PASSAGE_PREFIX + "text"]


def test_query_returns_one_vector() -> None:
    e, _ = _embedder()
    assert e.embed_query("minimum sip").shape == (DIM,)


def test_batches_of_32() -> None:
    session = FakeSession()
    e, _ = _embedder(session)
    assert e.embed_passages([f"t{i}" for i in range(70)]).shape == (70, DIM)
    assert [f["input_ids"].shape[0] for f in session.feeds] == [32, 32, 6]


def test_token_type_ids_sent_only_when_the_model_takes_them() -> None:
    with_types = FakeSession(("input_ids", "attention_mask", "token_type_ids"))
    e, _ = _embedder(with_types)
    e.embed_query("q")
    assert "token_type_ids" in with_types.feeds[0]
    without = FakeSession()
    e, _ = _embedder(without)
    e.embed_query("q")
    assert "token_type_ids" not in without.feeds[0]


def test_empty_passages() -> None:
    e, _ = _embedder()
    assert e.embed_passages([]).shape == (0, 0)


def test_make_embedder_picks_the_runtime() -> None:
    assert isinstance(make_embedder(Settings(embed_runtime="torch")), E5Embedder)
    onnx = make_embedder(Settings(embed_runtime="onnx", embed_threads=1))
    assert isinstance(onnx, OnnxE5Embedder)
    assert onnx.model_name == "intfloat/e5-small-v2"  # same model name: same index
    assert not onnx.loaded  # nothing downloaded until first use


@pytest.mark.model
@pytest.mark.skipif(
    os.environ.get("GROWWBUDDY_RUN_MODEL_TESTS") != "1",
    reason="needs the real embedding model; set GROWWBUDDY_RUN_MODEL_TESTS=1",
)
def test_onnx_vectors_match_torch() -> None:
    texts = ["exit load mid cap", "minimum SIP large cap", "silver fof benchmark"]
    torch_e, onnx_e = E5Embedder(), OnnxE5Embedder()
    for a, b in ((torch_e.embed_passages(texts), onnx_e.embed_passages(texts)),
                 (torch_e.embed_query(texts[0]), onnx_e.embed_query(texts[0]))):  # fmt: skip
        np.testing.assert_allclose(a, b, atol=1e-5)
