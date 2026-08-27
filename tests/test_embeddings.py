"""Tests for the spectral foundation-model embedding adapters."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pytest

from msmcp.models import backends
from msmcp.models.backends import (
    DreaMSInferenceEmbedder,
    EmbeddingBackendUnavailable,
    LSMMS2InferenceEmbedder,
    get_embedder,
)
from msmcp.models.embeddings import (
    EMBEDDING_DIM,
    DreaMSEmbedder,
    LSMMS2Embedder,
    SpectralEmbedder,
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
            # Instantiation is the point of the test; suppress both checkers.
            SpectralEmbedder()  # type: ignore[abstract]  # pyright: ignore[reportAbstractUsage]

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


class TestBackendResolution:
    """get_embedder must honour MSMCP_EMBEDDING_BACKEND and degrade gracefully."""

    def test_mock_mode_never_touches_real_backends(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MSMCP_EMBEDDING_BACKEND", "mock")
        assert get_embedder("dreams").backend == "mock"
        assert isinstance(get_embedder("dreams"), DreaMSEmbedder)
        assert isinstance(get_embedder("lsm-ms2"), LSMMS2Embedder)

    def test_auto_mode_falls_back_when_backend_unavailable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("MSMCP_EMBEDDING_BACKEND", raising=False)

        def unavailable() -> None:
            raise EmbeddingBackendUnavailable("The DreaMS package is not installed.")

        monkeypatch.setattr(
            backends.DreaMSInferenceEmbedder, "check_available", unavailable
        )
        embedder = get_embedder("dreams")
        assert isinstance(embedder, DreaMSEmbedder)
        assert embedder.backend == "mock"

    def test_hf_mode_raises_with_instructions_when_unavailable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MSMCP_EMBEDDING_BACKEND", "hf")

        def unavailable() -> None:
            raise EmbeddingBackendUnavailable("The DreaMS package is not installed.")

        monkeypatch.setattr(
            backends.DreaMSInferenceEmbedder, "check_available", unavailable
        )
        with pytest.raises(EmbeddingBackendUnavailable, match="DreaMS package"):
            get_embedder("dreams")

    def test_auto_mode_falls_back_for_lsm_ms2_without_checkpoint(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("MSMCP_EMBEDDING_BACKEND", raising=False)
        monkeypatch.delenv("MSMCP_LSM_MS2_CKPT", raising=False)
        embedder = get_embedder("lsm-ms2")
        assert isinstance(embedder, LSMMS2Embedder)
        assert embedder.backend == "mock"

    def test_unknown_mode_is_treated_as_auto(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MSMCP_EMBEDDING_BACKEND", "banana")
        # auto: backend unavailable -> deterministic mock, not an exception
        assert isinstance(get_embedder("dreams"), DreaMSEmbedder)


class TestDreaMSInferenceAdapter:
    """The real-inference pipeline, exercised hermetically with a stub model."""

    @staticmethod
    def _stub_pipeline(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
        captured: dict[str, Any] = {}

        def fake_load(_: Any) -> tuple[Any, Any]:
            return (object(), object())

        def fake_inference(api: Any, model: Any, mgf_path: Path) -> np.ndarray:
            captured["mgf_text"] = Path(mgf_path).read_text(encoding="utf-8")
            return np.full((1, EMBEDDING_DIM), 2.0, dtype=np.float32)

        monkeypatch.setattr(backends.DreaMSInferenceEmbedder, "_load_model", fake_load)
        monkeypatch.setattr(backends, "_run_dreams_inference", fake_inference)
        return captured

    def test_embedding_shape_dtype_and_norm(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._stub_pipeline(monkeypatch)
        emb = DreaMSInferenceEmbedder().embed_spectrum(
            _peaks(PEPTIDE_LIKE), precursor_mz=400.0
        )
        assert emb.shape == (EMBEDDING_DIM,)
        assert emb.dtype == np.float32
        assert float(np.linalg.norm(emb)) == pytest.approx(1.0, abs=1e-6)

    def test_mgf_contains_precursor_and_normalised_peaks(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured = self._stub_pipeline(monkeypatch)
        DreaMSInferenceEmbedder().embed_spectrum(
            _peaks(PEPTIDE_LIKE), precursor_mz=400.0
        )
        text = captured["mgf_text"]
        assert text.startswith("BEGIN IONS")
        assert "PEPMASS=400.000000" in text
        assert "CHARGE=1+" in text
        # intensities max-normalised: 40 / 100 -> 0.4, 100 / 100 -> 1.0
        assert "110.0713 0.4000" in text
        assert "120.0808 1.0000" in text
        assert text.strip().endswith("END IONS")

    def test_peaks_at_or_above_precursor_are_dropped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured = self._stub_pipeline(monkeypatch)
        peaks = [[110.0713, 40.0], [450.0, 90.0], [500.0, 100.0]]
        DreaMSInferenceEmbedder().embed_spectrum(_peaks(peaks), precursor_mz=400.0)
        text = captured["mgf_text"]
        assert "110.0713" in text
        assert "450.0" not in text
        assert "500.0" not in text

    def test_all_peaks_above_precursor_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._stub_pipeline(monkeypatch)
        with pytest.raises(ValueError, match="below the precursor"):
            DreaMSInferenceEmbedder().embed_spectrum(
                _peaks([[450.0, 90.0]]), precursor_mz=400.0
            )

    def test_precursor_none_uses_max_mz_proxy(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured = self._stub_pipeline(monkeypatch)
        DreaMSInferenceEmbedder().embed_spectrum(_peaks(PEPTIDE_LIKE))
        expected = f"PEPMASS={max(p[0] for p in PEPTIDE_LIKE):.6f}"
        assert expected in captured["mgf_text"]

    def test_dependency_stdout_is_redirected_to_stderr(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        def noisy_inference(api: Any, model: Any, mgf_path: Path) -> np.ndarray:
            print("DREAMS-DEBUG: tokenizing peaks")  # a dependency printing
            return np.ones((1, EMBEDDING_DIM), dtype=np.float32)

        monkeypatch.setattr(
            backends.DreaMSInferenceEmbedder,
            "_load_model",
            lambda _: (object(), object()),
        )
        monkeypatch.setattr(backends, "_run_dreams_inference", noisy_inference)
        DreaMSInferenceEmbedder().embed_spectrum(_peaks(PEPTIDE_LIKE))
        out, err = capsys.readouterr()
        assert out == ""  # stdio transport boundary preserved
        assert "DREAMS-DEBUG" in err

    def test_wrong_output_shape_raises(self) -> None:
        class FakeAPI:
            ContrastiveHead = object

            def dreams_predictions(self, **kwargs: Any) -> np.ndarray:
                return np.ones((1, 7), dtype=np.float32)

        with pytest.raises(ValueError, match="shape"):
            backends._run_dreams_inference(FakeAPI(), None, Path("spectrum.mgf"))

    def test_zero_embedding_vector_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def zero_inference(api: Any, model: Any, mgf_path: Path) -> np.ndarray:
            return np.zeros((1, EMBEDDING_DIM), dtype=np.float32)

        monkeypatch.setattr(
            backends.DreaMSInferenceEmbedder,
            "_load_model",
            lambda _: (object(), object()),
        )
        monkeypatch.setattr(backends, "_run_dreams_inference", zero_inference)
        with pytest.raises(ValueError, match="zero embedding"):
            DreaMSInferenceEmbedder().embed_spectrum(_peaks(PEPTIDE_LIKE))


class TestLSMMS2InferenceAdapter:
    """Bring-your-own-checkpoint LSM-MS2 adapter, exercised with a stub model."""

    def test_unavailable_without_checkpoint(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("MSMCP_LSM_MS2_CKPT", raising=False)
        with pytest.raises(EmbeddingBackendUnavailable, match="MSMCP_LSM_MS2_CKPT"):
            LSMMS2InferenceEmbedder.check_available()

    def test_missing_checkpoint_file_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MSMCP_LSM_MS2_CKPT", "/nonexistent/lsm.pt")
        with pytest.raises(EmbeddingBackendUnavailable, match="not found"):
            LSMMS2InferenceEmbedder()._load_model()

    def test_encode_pipeline_is_canonical_and_normalised(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class FakeLSM:
            def __init__(self) -> None:
                self.calls: list[tuple[np.ndarray, np.ndarray, float]] = []

            def encode(
                self, mz: np.ndarray, intensity: np.ndarray, precursor_mz: float
            ) -> np.ndarray:
                self.calls.append(
                    (
                        np.asarray(mz, dtype=np.float64),
                        np.asarray(intensity, dtype=np.float64),
                        precursor_mz,
                    )
                )
                return np.arange(1.0, 17.0, dtype=np.float32)  # 16-d output

        fake = FakeLSM()
        monkeypatch.setattr(
            backends.LSMMS2InferenceEmbedder, "_load_model", lambda _: fake
        )
        embedder = LSMMS2InferenceEmbedder()
        peaks = [[120.0, 50.0], [110.0, 100.0], [130.0, 25.0]]
        emb = embedder.embed_spectrum(_peaks(peaks), precursor_mz=500.0)

        assert emb.shape == (16,)
        assert emb.dtype == np.float32
        assert float(np.linalg.norm(emb)) == pytest.approx(1.0, abs=1e-6)
        assert embedder.embedding_dim == 16  # derived from the first encode()

        mz, intensity, prec = fake.calls[-1]
        assert list(mz) == [110.0, 120.0, 130.0]  # sorted ascending
        assert list(intensity) == pytest.approx([1.0, 0.5, 0.25])  # max-normalised
        assert prec == 500.0

    def test_zero_intensity_spectrum_skips_normalisation(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class ZeroLSM:
            def encode(
                self, mz: np.ndarray, intensity: np.ndarray, precursor_mz: float
            ) -> np.ndarray:
                return np.ones(8, dtype=np.float32)

        monkeypatch.setattr(
            backends.LSMMS2InferenceEmbedder, "_load_model", lambda _: ZeroLSM()
        )
        emb = LSMMS2InferenceEmbedder().embed_spectrum(
            _peaks([[110.0, 0.0], [120.0, 0.0]]), precursor_mz=200.0
        )
        assert emb.shape == (8,)
        assert float(np.linalg.norm(emb)) == pytest.approx(1.0, abs=1e-6)
