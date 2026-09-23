"""Spectral foundation-model adapters and scoring contracts for MSMCP.

This package hosts pluggable adapters for spectral embedding models and the
scoring contract the search pipeline is written against: the
:class:`~msmcp.models.embeddings.SpectralEmbedder` contract, test/dev-only
deterministic mocks, real PyTorch inference backends
(:mod:`msmcp.models.backends`), and the
:class:`~msmcp.models.scoring.SpectrumScorer` contract with its classical and
embedding implementations (:mod:`msmcp.models.scoring`).
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
from msmcp.models.scoring import (
    ClassicalScorer,
    EmbeddingScorer,
    SpectrumScorer,
    get_scorer,
)

__all__ = [
    "ClassicalScorer",
    "DreaMSEmbedder",
    "DreaMSInferenceEmbedder",
    "EmbeddingBackendUnavailable",
    "EmbeddingScorer",
    "LSMMS2Embedder",
    "LSMMS2InferenceEmbedder",
    "SpectralEmbedder",
    "SpectrumScorer",
    "get_embedder",
    "get_scorer",
]
