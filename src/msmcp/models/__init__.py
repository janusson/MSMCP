"""Spectral foundation-model adapters for MSMCP.

This package hosts pluggable adapters for spectral embedding models.  The
current implementations are deterministic development stand-ins; production
adapters subclass :class:`~msmcp.models.embeddings.SpectralEmbedder` and swap
in real PyTorch / HuggingFace inference behind the same interface.
"""

from msmcp.models.embeddings import (
    DreaMSEmbedder,
    LSMMS2Embedder,
    SpectralEmbedder,
    get_embedder,
)

__all__ = ["DreaMSEmbedder", "LSMMS2Embedder", "SpectralEmbedder", "get_embedder"]
