"""Embeddings: fastembed singleton (all-MiniLM-L6-v2, 384-dim).

The model name is load-bearing: worker and server MUST use the same model
(one EMBED_MODEL constant). Changing it invalidates all stored vectors —
run `python -m wellisearch.reindex`.
"""
from __future__ import annotations

import logging
import os
import pathlib
import threading
from typing import Any

from .config import get_settings

log = logging.getLogger("wellisearch.embed")

_model: Any = None
_lock = threading.Lock()


def model_name() -> str:
    """Normalize the configured model name to fastembed's key form.

    fastembed wants the org-qualified name (e.g.
    `sentence-transformers/all-MiniLM-L6-v2`); a bare model name gets the
    default org prefixed.
    """
    name = get_settings().EMBED_MODEL
    if "/" not in name:
        name = f"sentence-transformers/{name}"
    return name


def embed(texts: list[str]) -> list[list[float]]:
    """Embed a batch of texts (documents or queries)."""
    if not texts:
        return []
    m = _get_model()
    return [list(v) for v in m.embed(list(texts))]


def embed_one(text: str) -> list[float]:
    """Embed a single text and return its vector."""
    return embed([text])[0]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _cache_dir() -> str:
    """fastembed's model cache dir (FASTEMBED_CACHE_DIR, or ~/.cache/fastembed)."""
    return os.environ.get("FASTEMBED_CACHE_DIR") or str(pathlib.Path.home() / ".cache" / "fastembed")


def _get_model() -> Any:
    """Load the fastembed model once (thread-safe) and verify its dimension
    matches EMBED_DIMS."""
    global _model
    with _lock:
        if _model is None:
            from fastembed import TextEmbedding

            s = get_settings()
            log.info("loading embedding model %s (threads=%s, first use may download ~90 MB)…",
                     model_name(), s.EMBED_THREADS)
            _model = TextEmbedding(
                model_name=model_name(),
                cache_dir=_cache_dir(),
                threads=s.EMBED_THREADS,
            )
            dim = _model.embedding_size
            if dim != s.EMBED_DIMS:
                raise RuntimeError(
                    f"embedding dim mismatch: model={dim} EMBED_DIMS={s.EMBED_DIMS} — "
                    "the schema assumes 384-dim vectors"
                )
            log.info("embedding model ready (dim=%d)", dim)
    return _model
