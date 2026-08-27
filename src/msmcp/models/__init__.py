"""Spectral foundation-model adapters for MSMCP.

This package hosts pluggable adapters for spectral embedding models: the
:class:`~msmcp.models.embeddings.SpectralEmbedder` contract, deterministic
fallback implementations, and real PyTorch inference backends
(:mod:`msmcp.models.backends`).
"""

from msmcp.models.backends import (
    DreaMSInferenceEmbedder,
    EmbeddingBackendUnavailable,
    LSMMS2InferenceEmbedder,
    get_embedder,
)
from msmcp.models.embeddings import (
    DreaMSEmbedder,
    LSMMS2Embedder,
    SpectralEmbedder,
)

__all__ = [
    "DreaMSEmbedder",
    "DreaMSInferenceEmbedder",
    "EmbeddingBackendUnavailable",
    "LSMMS2Embedder",
    "LSMMS2InferenceEmbedder",
    "SpectralEmbedder",
    "get_embedder",
]
