"""Test doubles: a deterministic embedder and a scripted LLM (no model download, no API)."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from typing import Any

import numpy as np

from growwbuddy.llm.client import JsonResult
from growwbuddy.retrieval.embed import Vectors, normalise_rows

_WORD = re.compile(r"[a-z0-9]+")


class FakeEmbedder:
    """Hashed bag of words: texts sharing words get similar vectors. Counts its calls."""

    def __init__(self, model_name: str = "fake/bow-64", dim: int = 64) -> None:
        self._model_name = model_name
        self.dim = dim
        self.passage_calls = 0

    @property
    def model_name(self) -> str:
        return self._model_name

    def _vec(self, text: str) -> np.ndarray:
        v = np.zeros(self.dim, dtype=np.float32)
        for w in _WORD.findall(text.lower()):
            h = int.from_bytes(hashlib.md5(w.encode()).digest()[:4], "little")
            v[h % self.dim] += 1.0
        if not v.any():
            v[0] = 1.0
        return v

    def embed_passages(self, texts: Sequence[str]) -> Vectors:
        self.passage_calls += 1
        return normalise_rows(np.stack([self._vec(t) for t in texts]))

    def embed_query(self, text: str) -> Vectors:
        vec: Vectors = normalise_rows(self._vec(text))[0]
        return vec


class FakeLLM:
    """Returns the scripted results in order and records each request."""

    def __init__(self, *results: JsonResult) -> None:
        self.results = list(results)
        self.calls: list[dict[str, Any]] = []

    async def complete_json(
        self,
        *,
        system: str,
        user: str,
        schema_name: str,
        schema: dict[str, Any],
        max_tokens: int,
        kind: str = "call",
    ) -> JsonResult:
        self.calls.append(
            {"system": system, "user": user, "schema_name": schema_name, "kind": kind}
        )
        if not self.results:
            raise AssertionError("FakeLLM called more times than scripted")
        return self.results.pop(0)

    def kinds(self) -> list[str]:
        return [c["kind"] for c in self.calls]


def ok(data: dict[str, Any]) -> JsonResult:
    """A clean `stop` result carrying `data`."""
    return JsonResult(True, data=data, finish_reason="stop", usage={"total_tokens": 100})


def router_says(
    scheme: str = "none",
    intent: str = "factual",
    query: str = "question",
    may_be_advice: bool = False,
) -> JsonResult:
    return ok(
        {"intent": intent, "may_be_advice": may_be_advice, "scheme": scheme,
         "rewritten_query": query}
    )  # fmt: skip


def answer_says(*sentences: str, cite: str = "", answerable: bool = True) -> JsonResult:
    return ok({"answerable": answerable, "sentences": list(sentences), "source_chunk_id": cite})


class FailIfCalledLLM:
    async def complete_json(self, **_: Any) -> JsonResult:
        raise AssertionError("the LLM must not be called")
