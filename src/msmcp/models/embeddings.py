"""Spectral foundation-model embedding adapters.

This module defines the :class:`SpectralEmbedder` contract and the
deterministic, fixed-grid fallback implementations (DreaMS / LSM-MS2) that
keep the server fully operational without ML dependencies.  Real PyTorch
inference and the :func:`msmcp.models.backends.get_embedder` resolver live
in :mod:`msmcp.models.backends`; the resolver picks between the
deterministic implementations and real inference based on the
``MSMCP_EMBEDDING_BACKEND`` environment variable (``mock`` | ``auto`` |
``hf``), falling back to the deterministic implementations when a real
backend is unavailable so agentic workflows always keep working.

Embedding scheme (deterministic fallback)
-----------------------------------------
1. Peaks are projected onto a fixed m/z grid (``EMBEDDING_DIM`` bins spanning
   ``MZ_SPAN`` Da) with per-model intensity compression (sqrt for DreaMS,
   log1p for LSM-MS2).
2. Small deterministic noise is applied, seeded by a hash of the precursor
   m/z and the peak content.  Each model uses a distinct salt, so every model
   lives in its own embedding space.
3. The vector is L2-normalised, so cosine similarity between two embeddings
   is a plain dot product and identical spectra score exactly 1.0.
"""

from __future__ import annotations

import hashlib
import struct
from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import ClassVar

import numpy as np

EMBEDDING_DIM: int = 1024
"""Dimensionality of the embedding vectors produced by the adapters."""

MZ_SPAN: float = 2048.0
"""Upper m/z bound of the fixed projection grid (Da)."""

_NOISE_SIGMA: float = 0.05
"""Scale of the deterministic per-bin noise (relative, multiplicative)."""

_MAX_SEED_PEAKS: int = 256
"""Maximum number of peaks hashed into the embedding seed (CPU efficiency)."""


def _coerce_peaks(peaks: np.ndarray) -> np.ndarray:
    """Validate *peaks* and return it as a float64 (N, 2) array."""
    arr = np.asarray(peaks, dtype=np.float64)
    if arr.ndim != 2 or arr.shape[1] != 2 or arr.shape[0] == 0:
        raise ValueError(
            "peaks must be a non-empty (N, 2) array of [m/z, intensity] pairs; "
            f"got shape {arr.shape}"
        )
    return arr


def _precursor_proxy(peaks: np.ndarray) -> float:
    """Derive a deterministic precursor stand-in from the peak list.

    In MS2 spectra the precursor is frequently retained as the highest m/z
    peak, so the max peak m/z is used when no explicit precursor is supplied.
    """
    return float(peaks[:, 0].max())


def _spectrum_seed(precursor_mz: float, peaks: np.ndarray, salt: bytes) -> int:
    """Derive a deterministic integer seed from precursor m/z and peak content.

    Peaks are sorted (m/z, then intensity) before hashing so that the seed is
    independent of peak ordering within the list.
    """
    digest = hashlib.blake2b(salt, digest_size=8)
    digest.update(struct.pack("<d", float(precursor_mz)))
    order = np.lexsort((peaks[:, 1], peaks[:, 0]))
    for idx in order[:_MAX_SEED_PEAKS]:
        digest.update(struct.pack("<dd", peaks[idx, 0], peaks[idx, 1]))
    return int.from_bytes(digest.digest(), "little")


def _compute_embedding(
    peaks: np.ndarray,
    precursor_mz: float,
    salt: bytes,
    compress: Callable[[np.ndarray], np.ndarray],
) -> np.ndarray:
    """Project *peaks* into a normalised float32 embedding vector."""
    bin_width = MZ_SPAN / EMBEDDING_DIM
    bins = np.clip((peaks[:, 0] / bin_width).astype(np.int64), 0, EMBEDDING_DIM - 1)

    hist = np.zeros(EMBEDDING_DIM, dtype=np.float64)
    np.add.at(hist, bins, compress(peaks[:, 1]))

    rng = np.random.default_rng(_spectrum_seed(precursor_mz, peaks, salt))
    noise = rng.normal(0.0, _NOISE_SIGMA, EMBEDDING_DIM)
    vector = hist * (1.0 + noise)

    norm = float(np.linalg.norm(vector))
    if norm > 0.0:
        vector /= norm
    return vector.astype(np.float32)


class SpectralEmbedder(ABC):
    """Abstract base class for spectral foundation-model embedders.

    Implementations convert an (N, 2) peak list into a deterministic,
    L2-normalised float32 embedding vector that captures whole-spectrum
    fragmentation patterns for deep similarity scoring.
    """

    name: ClassVar[str] = "SpectralEmbedder"
    """Human-readable model name used in tool reports."""

    backend: ClassVar[str] = "mock"
    """Execution backend: ``"mock"`` (deterministic fallback) or ``"hf"``."""

    embedding_dim: int = EMBEDDING_DIM
    """Dimensionality of the vectors returned by :meth:`embed_spectrum`.

    Class-level default; adapters whose real model has a different
    dimensionality overwrite it on the instance after loading.
    """

    @staticmethod
    @abstractmethod
    def check_available() -> None:
        """Verify the backend can be loaded without raising.

        Deterministic fallbacks are always available; real-inference
        backends raise
        :class:`~msmcp.models.backends.EmbeddingBackendUnavailable` when the
        model package or checkpoint is missing.
        """

    @abstractmethod
    def embed_spectrum(
        self, peaks: np.ndarray, precursor_mz: float | None = None
    ) -> np.ndarray:
        """Return a deterministic embedding for *peaks*.

        Parameters
        ----------
        peaks
            (N, 2) array of [m/z, intensity] peak pairs.
        precursor_mz
            Precursor m/z of the spectrum.  When ``None``, implementations
            fall back to a deterministic proxy derived from the peak list.

        Returns
        -------
        np.ndarray
            L2-normalised ``(embedding_dim,)`` float32 vector.
        """


class DreaMSEmbedder(SpectralEmbedder):
    """DreaMS-style embedder (deterministic fallback).

    Used when the real DreaMS transformer package is not installed (or the
    ``MSMCP_EMBEDDING_BACKEND=mock`` mode is selected).  The fallback
    projects peaks onto the fixed m/z grid with sqrt intensity compression
    and applies content-seeded noise, sharing the interface (and the
    determinism contract) of the real adapter.
    """

    name: ClassVar[str] = "DreaMS"
    _SALT: ClassVar[bytes] = b"msmcp/dreams/v1"

    @staticmethod
    def check_available() -> None:
        """Deterministic fallbacks are always available."""

    def embed_spectrum(
        self, peaks: np.ndarray, precursor_mz: float | None = None
    ) -> np.ndarray:
        peaks_arr = _coerce_peaks(peaks)
        precursor = (
            precursor_mz if precursor_mz is not None else _precursor_proxy(peaks_arr)
        )
        return _compute_embedding(peaks_arr, precursor, self._SALT, np.sqrt)


class LSMMS2Embedder(SpectralEmbedder):
    """LSM-MS2-style embedder (deterministic fallback).

    Used when no real LSM-MS2 checkpoint is configured (no public inference
    weights exist upstream; see :class:`~msmcp.models.backends.LSMMS2InferenceEmbedder`).
    The fallback mirrors the adapter contract with log1p intensity
    compression and an LSM-MS2-specific embedding salt.
    """

    name: ClassVar[str] = "LSM-MS2"
    _SALT: ClassVar[bytes] = b"msmcp/lsm-ms2/v1"

    @staticmethod
    def check_available() -> None:
        """Deterministic fallbacks are always available."""

    def embed_spectrum(
        self, peaks: np.ndarray, precursor_mz: float | None = None
    ) -> np.ndarray:
        peaks_arr = _coerce_peaks(peaks)
        precursor = (
            precursor_mz if precursor_mz is not None else _precursor_proxy(peaks_arr)
        )
        return _compute_embedding(peaks_arr, precursor, self._SALT, np.log1p)
