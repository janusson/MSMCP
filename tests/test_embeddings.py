"""Tests for the spectral foundation-model embedding adapters."""

from __future__ import annotations

import numpy as np
import pytest

from msmcp.models.embeddings import (
    EMBEDDING_DIM,
    DreaMSEmbedder,
    LSMMS2Embedder,
    SpectralEmbedder,
    get_embedder,
)
from msmcp.tools.similarity import _cosine

PEPTIDE_LIKE = [
    [110.0713, 40.0],
    [120.0808, 100.0],
    [136.0757, 60.0],
    [175.1190, 80.0],
    [223.1077, 30.0],
]
SHARED_PEAKS = [[110.0713, 40.0], [120.0808, 100.0], [136.0757, 60.0]]
SUPERSET_PEAKS = [
    [110.0713, 40.0],
    [120.0808, 100.0],
    [136.0757, 60.0],
    [500.1, 55.0],
    [700.2, 45.0],
]
DISJOINT_PEAKS = [[900.1, 90.0], [950.2, 70.0]]


def _peaks(rows: list[list[float]]) -> np.ndarray:
    return np.asarray(rows, dtype=np.float64)


class TestEmbedderContract:
    """Both concrete embedders must honour the SpectralEmbedder contract."""

    def test_abc_cannot_be_instantiated(self) -> None:
        with pytest.raises(TypeError):
            SpectralEmbedder()  # type: ignore[abstract]

    @pytest.mark.parametrize("cls", [DreaMSEmbedder, LSMMS2Embedder])
    def test_shape_and_dtype(self, cls: type[SpectralEmbedder]) -> None:
        emb = cls().embed_spectrum(_peaks(PEPTIDE_LIKE))
        assert emb.shape == (EMBEDDING_DIM,)
        assert emb.dtype == np.float32

    @pytest.mark.parametrize("cls", [DreaMSEmbedder, LSMMS2Embedder])
    def test_unit_norm(self, cls: type[SpectralEmbedder]) -> None:
        emb = cls().embed_spectrum(_peaks(PEPTIDE_LIKE))
        assert float(np.linalg.norm(emb)) == pytest.approx(1.0, abs=1e-6)

    @pytest.mark.parametrize("cls", [DreaMSEmbedder, LSMMS2Embedder])
    def test_deterministic(self, cls: type[SpectralEmbedder]) -> None:
        a = cls().embed_spectrum(_peaks(PEPTIDE_LIKE))
        b = cls().embed_spectrum(_peaks(PEPTIDE_LIKE))
        np.testing.assert_array_equal(a, b)

    @pytest.mark.parametrize("cls", [DreaMSEmbedder, LSMMS2Embedder])
    def test_precursor_mz_influences_embedding(
        self, cls: type[SpectralEmbedder]
    ) -> None:
        peaks = _peaks(PEPTIDE_LIKE)
        a = cls().embed_spectrum(peaks, precursor_mz=400.0)
        b = cls().embed_spectrum(peaks, precursor_mz=600.0)
        assert not np.allclose(a, b)
        assert _cosine(a, b) < 1.0

    @pytest.mark.parametrize("cls", [DreaMSEmbedder, LSMMS2Embedder])
    def test_peak_order_is_irrelevant(self, cls: type[SpectralEmbedder]) -> None:
        a = cls().embed_spectrum(_peaks(PEPTIDE_LIKE))
        b = cls().embed_spectrum(_peaks(list(reversed(PEPTIDE_LIKE))))
        np.testing.assert_array_equal(a, b)

    @pytest.mark.parametrize("cls", [DreaMSEmbedder, LSMMS2Embedder])
    @pytest.mark.parametrize("bad", [np.empty((0, 2)), np.zeros((3, 3)), np.zeros(4)])
    def test_invalid_peak_array_raises(
        self, cls: type[SpectralEmbedder], bad: np.ndarray
    ) -> None:
        with pytest.raises(ValueError, match="non-empty"):
            cls().embed_spectrum(bad)


class TestEmbeddingSemantics:
    """Embeddings should reflect spectral content in a graded fashion."""

    def test_identical_spectra_score_one(self) -> None:
        peaks = _peaks(PEPTIDE_LIKE)
        embedder = DreaMSEmbedder()
        assert _cosine(
            embedder.embed_spectrum(peaks), embedder.embed_spectrum(peaks)
        ) == pytest.approx(1.0)

    def test_shared_peaks_score_higher_than_disjoint(self) -> None:
        embedder = LSMMS2Embedder()
        s_shared = _cosine(
            embedder.embed_spectrum(_peaks(SHARED_PEAKS)),
            embedder.embed_spectrum(_peaks(SUPERSET_PEAKS)),
        )
        s_disjoint = _cosine(
            embedder.embed_spectrum(_peaks(SHARED_PEAKS)),
            embedder.embed_spectrum(_peaks(DISJOINT_PEAKS)),
        )
        assert s_shared > 0.5  # substantial overlap, graded not binary
        assert s_disjoint == pytest.approx(0.0, abs=1e-6)
        assert s_shared > s_disjoint

    def test_different_models_use_different_spaces(self) -> None:
        peaks = _peaks(PEPTIDE_LIKE)
        a = DreaMSEmbedder().embed_spectrum(peaks)
        b = LSMMS2Embedder().embed_spectrum(peaks)
        assert not np.allclose(a, b)
        assert _cosine(a, b) < 0.999


class TestGetEmbedder:
    def test_registry_resolves_known_methods(self) -> None:
        assert isinstance(get_embedder("dreams"), DreaMSEmbedder)
        assert isinstance(get_embedder("lsm-ms2"), LSMMS2Embedder)

    def test_unknown_method_raises(self) -> None:
        with pytest.raises(ValueError, match="dreams, lsm-ms2"):
            get_embedder("specter2")
