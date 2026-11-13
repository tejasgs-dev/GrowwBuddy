"""Embedders (task 2.3).

The default `E5Embedder` uses `intfloat/e5-small-v2` (384 dims, CPU), chosen in the Phase 2
data research. e5 models need the `query: ` / `passage: ` prefixes. Vectors are
L2-normalised, so a dot product is the cosine similarity.

Tests use a fake embedder, so CI never downloads the model.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, Protocol

import numpy as np
import numpy.typing as npt

if TYPE_CHECKING:
    from growwbuddy.config import Settings

Vectors = npt.NDArray[np.float32]


class EmbedderUnavailableError(RuntimeError):
    """The embedding model could not be loaded (EC-ING-24)."""


class Embedder(Protocol):
    @property
    def model_name(self) -> str: ...

    def embed_passages(self, texts: Sequence[str]) -> Vectors:
        """Shape (n, dim), float32, each row L2-normalised."""
        ...

    def embed_query(self, text: str) -> Vectors:
        """Shape (dim,), float32, L2-normalised."""
        ...


def normalise_rows(m: npt.NDArray[Any]) -> Vectors:
    m = np.asarray(m, dtype=np.float32)
    if m.ndim == 1:
        m = m[None, :]
    norms = np.linalg.norm(m, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    out: Vectors = (m / norms).astype(np.float32)
    return out


class E5Embedder:
    """`intfloat/e5-*` models via sentence-transformers. Loads the model once, on first use."""

    QUERY_PREFIX = "query: "
    PASSAGE_PREFIX = "passage: "

    def __init__(self, model_name: str = "intfloat/e5-small-v2", device: str = "cpu") -> None:
        self._model_name = model_name
        self._device = device
        self._model: Any = None

    @property
    def model_name(self) -> str:
        return self._model_name

    def _load(self) -> Any:
        if self._model is None:
            try:
                from sentence_transformers import SentenceTransformer

                self._model = SentenceTransformer(self._model_name, device=self._device)
            except Exception as e:  # network, disk, or a bad model name
                raise EmbedderUnavailableError(
                    f"could not load embedding model '{self._model_name}': {e}\n"
                    "The model is downloaded once (~130 MB) on the first ingest and then "
                    "cached. Connect to the internet and run `make ingest` again, or set "
                    "GROWWBUDDY_EMBED_MODEL to a model that is already cached."
                ) from e
        return self._model

    def _encode(self, texts: list[str]) -> Vectors:
        vecs = self._load().encode(
            texts, batch_size=32, normalize_embeddings=True, convert_to_numpy=True
        )
        return normalise_rows(vecs)

    def embed_passages(self, texts: Sequence[str]) -> Vectors:
        if not texts:
            return np.zeros((0, 0), dtype=np.float32)
        return self._encode([self.PASSAGE_PREFIX + t for t in texts])

    def embed_query(self, text: str) -> Vectors:
        vec: Vectors = self._encode([self.QUERY_PREFIX + text])[0]
        return vec


def make_embedder(s: Settings | None = None) -> Embedder:
    if s is None:
        from growwbuddy.config import settings

        s = settings
    if "e5" not in s.embed_model.lower():
        # Other model families need different prefixes; add an Embedder for them first.
        raise EmbedderUnavailableError(
            f"no embedder for '{s.embed_model}'; only intfloat/e5-* models are supported"
        )
    return E5Embedder(s.embed_model)
