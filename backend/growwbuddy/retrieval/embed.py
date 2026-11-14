"""Embedders (task 2.3).

The default `E5Embedder` uses `intfloat/e5-small-v2` (384 dims, CPU), chosen in the Phase 2
data research. e5 models need the `query: ` / `passage: ` prefixes. Vectors are
L2-normalised, so a dot product is the cosine similarity. `OnnxE5Embedder` runs the same
model without torch (`GROWWBUDDY_EMBED_RUNTIME=onnx`) for small hosts.

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
    """`intfloat/e5-*` models via sentence-transformers. Loads the model once: at server
    startup (`warm_up`), or on first use elsewhere (ingest, CLI)."""

    QUERY_PREFIX = "query: "
    PASSAGE_PREFIX = "passage: "

    def __init__(self, model_name: str = "intfloat/e5-small-v2", device: str = "cpu") -> None:
        self._model_name = model_name
        self._device = device
        self._model: Any = None

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def loaded(self) -> bool:
        return self._model is not None

    def warm_up(self) -> None:
        """Load the model and run one query, so the first real question doesn't wait for
        it (~25 s on a cold start: importing torch and sentence-transformers, plus the
        Hugging Face Hub checks). The server calls this before it reports ready."""
        self.embed_query("warm up")

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


class OnnxE5Embedder:
    """The same `intfloat/e5-*` model run with ONNX Runtime instead of torch
    (`GROWWBUDDY_EMBED_RUNTIME=onnx`), for small hosts: the server then never imports torch.

    Uses the fp32 `model.onnx` the model repository publishes, and repeats what the
    sentence-transformers pipeline does for e5 (the repo's modules.json): tokenise with
    truncation at 512 tokens, mean-pool the last hidden state over the attention mask,
    L2-normalise. The vectors match the torch ones to float precision, so an index built
    by either runtime works with the other and `model_name` stays the same.
    """

    QUERY_PREFIX = E5Embedder.QUERY_PREFIX
    PASSAGE_PREFIX = E5Embedder.PASSAGE_PREFIX
    MAX_TOKENS = 512  # the repo's sentence_bert_config.json max_seq_length
    BATCH = 32

    def __init__(self, model_name: str = "intfloat/e5-small-v2", threads: int = 0) -> None:
        self._model_name = model_name
        self._threads = threads  # 0: ONNX Runtime's default (one per core)
        self._session: Any = None
        self._tokenizer: Any = None

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def loaded(self) -> bool:
        return self._session is not None

    def warm_up(self) -> None:
        """Load the model and run one query (see E5Embedder.warm_up)."""
        self.embed_query("warm up")

    def _load(self) -> tuple[Any, Any]:
        if self._session is None:
            try:
                import onnxruntime as ort
                from huggingface_hub import hf_hub_download
                from tokenizers import Tokenizer

                tok = Tokenizer.from_file(hf_hub_download(self._model_name, "tokenizer.json"))
                tok.enable_truncation(max_length=self.MAX_TOKENS)
                tok.enable_padding()
                opts = ort.SessionOptions()
                if self._threads > 0:
                    opts.intra_op_num_threads = self._threads
                    opts.inter_op_num_threads = 1
                session = ort.InferenceSession(
                    hf_hub_download(self._model_name, "model.onnx"),
                    sess_options=opts,
                    providers=["CPUExecutionProvider"],
                )
            except Exception as e:  # network, disk, a bad model name, or no ONNX export
                raise EmbedderUnavailableError(
                    f"could not load ONNX embedding model '{self._model_name}': {e}\n"
                    "The model repository must publish model.onnx and tokenizer.json. "
                    "Connect to the internet once to download them, or set "
                    "GROWWBUDDY_EMBED_RUNTIME=torch."
                ) from e
            self._tokenizer, self._session = tok, session
        return self._tokenizer, self._session

    def _encode(self, texts: list[str]) -> Vectors:
        tok, session = self._load()
        wanted = {i.name for i in session.get_inputs()}
        out: list[Vectors] = []
        for start in range(0, len(texts), self.BATCH):
            enc = tok.encode_batch(texts[start : start + self.BATCH])
            ids = np.array([e.ids for e in enc], dtype=np.int64)
            mask = np.array([e.attention_mask for e in enc], dtype=np.int64)
            feeds = {"input_ids": ids, "attention_mask": mask}
            if "token_type_ids" in wanted:
                feeds["token_type_ids"] = np.array([e.type_ids for e in enc], dtype=np.int64)
            hidden = session.run(None, feeds)[0]  # (batch, tokens, dim)
            m = mask[:, :, None].astype(np.float32)
            pooled = (hidden * m).sum(axis=1) / np.clip(m.sum(axis=1), 1e-9, None)
            out.append(normalise_rows(pooled))
        return np.concatenate(out, axis=0)

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
    if s.embed_runtime == "onnx":
        return OnnxE5Embedder(s.embed_model, threads=s.embed_threads)
    return E5Embedder(s.embed_model)
