"""Spectral scoring contracts and implementations.

This module defines the :class:`SpectrumScorer` contract — "how alike are these
two peak lists?" — and the two implementations MSMCP ships: greedy classical
matching and foundation-model embeddings.  It exists so that the scan and its
null model are written once and can be pointed at any scorer, including a
third-party one (matchms, in the plan of record), without touching the pipeline.

Semantics every implementation must preserve
--------------------------------------------
1. **The score is not normalised over the matched peaks alone.**  Intensity the
   match does not explain has to lower the score.  Normalising over matched peaks
   makes a spectrum that shares *one* coincidental peak score exactly 1.0, which
   is indistinguishable from an identical spectrum; this defect was reproduced
   against the synthetic library (53 spectra scored 1.0, 52 of them sharing one
   incidental peak).
2. ``1.0`` means "the matched peaks carry all of the intensity in both spectra",
   never "these are the same compound".
3. An empty spectrum on either side scores ``0.0``, and a spectrum whose
   intensity is entirely zero scores ``0.0`` — never ``nan``.
4. Implementations are pure and thread-safe: no I/O, no mutation of the inputs,
   no state carried between calls.  The scan runs on a worker thread.

Cost is part of the contract
----------------------------
The null model scores a fixed ``NULL_SAMPLE_SIZE`` decoy sample per query — a
constant set by :mod:`msmcp.tools.search`, not a multiple of the library — so a
scorer's per-pair cost *is* the search cost.  :meth:`SpectrumScorer.score_many`
exists for that reason: it is the method the null model calls, and it is where an
implementation amortises query-side work (the classical scorer prepares the
query's norms once; the embedding scorer embeds the query once instead of once
per reference).
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from bisect import bisect_left, bisect_right
from collections.abc import Iterable, Sequence
from typing import NamedTuple

import numpy as np

from msmcp.instruments import get_profile
from msmcp.models.backends import get_embedder

PeakList = Sequence[tuple[float, float]]
"""Peaks as ``(m/z, intensity)`` pairs.  Order is not significant."""


def _validate_peak_list(peaks: Sequence[Sequence[float]], label: str) -> None:
    """Reject values outside the non-negative finite cosine-score domain."""
    for index, peak in enumerate(peaks):
        if len(peak) != 2:
            raise ValueError(f"{label} peak [{index}] must contain m/z and intensity")
        mz, intensity = float(peak[0]), float(peak[1])
        if not math.isfinite(mz):
            raise ValueError(f"{label} peak [{index}] has non-finite m/z ({mz!r})")
        if not math.isfinite(intensity):
            raise ValueError(
                f"{label} peak [{index}] has non-finite intensity ({intensity!r})"
            )
        if intensity < 0:
            raise ValueError(
                f"{label} peak [{index}] has negative intensity ({intensity})"
            )


DEFAULT_TOLERANCE: float = get_profile(None).ms2_tolerance_da
"""Default m/z matching window (Da) for the classical scorer.

The value is not a literal here: it is the generic instrument class's documented
default from :mod:`msmcp.instruments` (±20 mDa, MSMCP's unit-resolution MS2
window), so the one place a tolerance default lives is the instrument-class
table.  ``get_scorer``/``ClassicalScorer`` accept an explicit window from that
table (or from a caller) when one is known.
"""


class SpectrumScorer(ABC):
    """Score one peak list against another in ``[0, 1]``.

    Subclasses set :attr:`name` (the value accepted on the wire) and implement
    :meth:`score`.  Overriding :meth:`score_many` is optional but strongly
    encouraged when query-side work can be hoisted out of the loop.
    """

    name: str = "abstract"

    def __init__(self, tolerance: float = DEFAULT_TOLERANCE) -> None:
        if tolerance <= 0:
            raise ValueError(f"tolerance must be positive, got {tolerance!r}")
        self.tolerance = float(tolerance)

    @abstractmethod
    def score(self, query: PeakList, reference: PeakList) -> float:
        """Score one pair of spectra.  ``0.0`` for an empty or all-zero side."""

    def score_many(
        self,
        query: PeakList,
        references: Iterable[PeakList],
    ) -> list[float]:
        """Score *query* against many references, in order.

        The default loops over :meth:`score`.  See the module docstring: this is
        the hot path of a library search, because the null model calls it once per
        decoy.
        """
        return [self.score(query, reference) for reference in references]

    def __repr__(self) -> str:
        return f"{type(self).__name__}(tolerance={self.tolerance:g})"


# ---------------------------------------------------------------------------
# Classical: greedy one-to-one matching, normalised over all peaks
# ---------------------------------------------------------------------------
class _PreparedReference(NamedTuple):
    """A reference spectrum with its intensities L2-normalised."""

    mz: list[float]
    intensity: list[float]


def _normalise_intensities(
    peaks: Iterable[tuple[float, float]],
) -> list[tuple[float, float]]:
    """Scale intensities safely before computing their L2-normalised values."""
    pairs = [(float(mz), float(intensity)) for mz, intensity in peaks]
    scale = max((intensity for _mz, intensity in pairs), default=0.0)
    if scale == 0.0:
        return [(mz, 0.0) for mz, _intensity in pairs]
    scaled_norm = math.sqrt(
        math.fsum((intensity / scale) ** 2 for _mz, intensity in pairs)
    )
    return [(mz, (intensity / scale) / scaled_norm) for mz, intensity in pairs]


def _prepare_reference(peaks: PeakList) -> _PreparedReference:
    """Sort and L2-normalise a reference spectrum for window searches."""
    pairs = _normalise_intensities(sorted(peaks, key=lambda peak: peak[0]))
    return _PreparedReference(
        mz=[mz for mz, _ in pairs],
        intensity=[intensity for _, intensity in pairs],
    )


class ClassicalScorer(SpectrumScorer):
    """Greedy peak matching within an m/z window, cosine over all peaks.

    Query peaks are visited **in the order supplied**; each is matched to the
    nearest not-yet-used reference peak within ``tolerance`` Da.  The numerator is
    the dot product of the matched intensities.  The denominator is the product of
    the two *full* intensity norms — see the module docstring, point 1.

    Matching is greedy from the query's side, so it is not exactly symmetric:
    ``score(a, b)`` and ``score(b, a)`` may differ when two query peaks compete
    for one reference peak.  The pipeline always calls it query-first, and the
    score is reported as "the fraction of the query the reference explains".
    """

    def __init__(self, tolerance: float = DEFAULT_TOLERANCE) -> None:
        super().__init__(tolerance)
        self.name = "classical"

    def score(self, query: PeakList, reference: PeakList) -> float:
        _validate_peak_list(query, "query")
        _validate_peak_list(reference, "reference")
        normalised_query = _normalise_intensities(query)
        if not any(intensity for _mz, intensity in normalised_query):
            return 0.0
        prepared = _prepare_reference(reference)
        if not any(prepared.intensity):
            return 0.0
        return min(1.0, _matched_dot(normalised_query, prepared, self.tolerance))

    def score_many(
        self,
        query: PeakList,
        references: Iterable[PeakList],
    ) -> list[float]:
        """Score the query against many references, preparing it only once."""
        _validate_peak_list(query, "query")
        normalised_query = _normalise_intensities(query)
        query_has_signal = any(intensity for _mz, intensity in normalised_query)
        tolerance = self.tolerance
        scores: list[float] = []
        for reference in references:
            _validate_peak_list(reference, "reference")
            if not query_has_signal:
                scores.append(0.0)
                continue
            prepared = _prepare_reference(reference)
            if not any(prepared.intensity):
                scores.append(0.0)
                continue
            scores.append(min(1.0, _matched_dot(normalised_query, prepared, tolerance)))
        return scores


def _matched_dot(
    query: PeakList,
    reference: _PreparedReference,
    tolerance: float,
) -> float:
    """Dot product of the greedily matched L2-normalised intensities.

    One-to-one matching, nearest reference peak first, each reference peak used at
    most once. Intensities are normalised before multiplication to avoid overflow
    for finite values near the floating-point limit. Written with ``bisect`` and
    a byte array rather than array maths:
    at MS2 peak counts (tens of peaks) the numpy call overhead dominates the
    arithmetic, and this function is called once per decoy.
    """
    ref_mz = reference.mz
    ref_intensity = reference.intensity
    used = bytearray(len(ref_mz))
    matched_products: list[float] = []
    for query_mz, query_intensity in query:
        lo = bisect_left(ref_mz, query_mz - tolerance)
        hi = bisect_right(ref_mz, query_mz + tolerance)
        if lo >= hi:
            continue
        best_index = -1
        best_distance = float("inf")
        for index in range(lo, hi):
            if used[index]:
                continue
            distance = abs(ref_mz[index] - query_mz)
            if distance < best_distance:
                best_distance = distance
                best_index = index
        if best_index >= 0:
            used[best_index] = 1
            matched_products.append(float(query_intensity) * ref_intensity[best_index])
    return math.fsum(matched_products)


# ---------------------------------------------------------------------------
# Foundation-model embeddings
# ---------------------------------------------------------------------------
def _vector_cosine(a: np.ndarray, b: np.ndarray) -> float:
    """Cosine similarity between two non-negative embedding vectors."""
    norm_a = float(np.linalg.norm(a))
    norm_b = float(np.linalg.norm(b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return float(np.dot(a, b) / (norm_a * norm_b))


class EmbeddingScorer(SpectrumScorer):
    """Cosine similarity between foundation-model embeddings of whole spectra.

    The model is resolved through :func:`~msmcp.models.backends.get_embedder`, so
    production requires real inference; the deterministic mocks are reachable only
    under the explicit ``MSMCP_EMBEDDING_BACKEND=mock`` flag and are labelled as
    mocks everywhere they surface.

    :meth:`score_many` embeds the query **once** and reuses it across references.
    That is not a micro-optimisation: without it the null model would run the
    model 40x the library size times per search, for a single query.
    """

    def __init__(self, method: str, tolerance: float = DEFAULT_TOLERANCE) -> None:
        super().__init__(tolerance)
        self.name = method
        self._embedder = get_embedder(method)
        self.backend_label = self._embedder.backend_label
        self.embedding_dim = self._embedder.embedding_dim

    def _embed(self, peaks: PeakList) -> np.ndarray | None:
        if not peaks:
            return None
        return self._embedder.embed_spectrum(np.asarray(peaks, dtype=np.float64))

    def score(self, query: PeakList, reference: PeakList) -> float:
        _validate_peak_list(query, "query")
        _validate_peak_list(reference, "reference")
        query_vector = self._embed(query)
        reference_vector = self._embed(reference)
        if query_vector is None or reference_vector is None:
            return 0.0
        return _vector_cosine(query_vector, reference_vector)

    def score_many(
        self,
        query: PeakList,
        references: Iterable[PeakList],
    ) -> list[float]:
        _validate_peak_list(query, "query")
        query_vector = self._embed(query)
        if query_vector is None:
            return [0.0 for _ in references]
        query_norm = float(np.linalg.norm(query_vector))
        if query_norm == 0.0:
            return [0.0 for _ in references]
        scores: list[float] = []
        for reference in references:
            _validate_peak_list(reference, "reference")
            reference_vector = self._embed(reference)
            if reference_vector is None:
                scores.append(0.0)
                continue
            reference_norm = float(np.linalg.norm(reference_vector))
            if reference_norm == 0.0:
                scores.append(0.0)
                continue
            scores.append(
                float(
                    np.dot(query_vector, reference_vector)
                    / (query_norm * reference_norm)
                )
            )
        return scores


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------
_SCORER_REGISTRY: dict[str, type[SpectrumScorer]] = {
    "classical": ClassicalScorer,
}
"""Scorers addressable by name alone.  Embedding scorers are method-keyed and are
constructed through :class:`EmbeddingScorer` with the embedding method name."""

EMBEDDING_SCORER_METHODS: tuple[str, ...] = ("dreams", "lsm-ms2")
"""Scoring methods backed by an embedding model rather than peak algebra."""


def get_scorer(
    scoring_method: str,
    tolerance: float = DEFAULT_TOLERANCE,
) -> SpectrumScorer:
    """Instantiate the scorer registered under *scoring_method*.

    ``"classical"`` returns a :class:`ClassicalScorer`; the embedding methods
    return an :class:`EmbeddingScorer` around the matching model, which raises
    :class:`~msmcp.models.backends.EmbeddingBackendUnavailable` when the model is
    unavailable in production.

    Raises
    ------
    ValueError
        If *scoring_method* is not one of the known names.
    """
    if scoring_method in _SCORER_REGISTRY:
        return _SCORER_REGISTRY[scoring_method](tolerance)
    if scoring_method in EMBEDDING_SCORER_METHODS:
        return EmbeddingScorer(scoring_method, tolerance)
    known = ", ".join(sorted((*_SCORER_REGISTRY, *EMBEDDING_SCORER_METHODS)))
    raise ValueError(
        f"Unknown scoring method {scoring_method!r}; expected one of: {known}"
    )


__all__ = [
    "DEFAULT_TOLERANCE",
    "EMBEDDING_SCORER_METHODS",
    "ClassicalScorer",
    "EmbeddingScorer",
    "PeakList",
    "SpectrumScorer",
    "get_scorer",
]
