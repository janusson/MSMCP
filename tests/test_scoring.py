"""Contract tests for the spectral scoring interface.

These assert the *properties* a scorer must have, independently of the scan:

* the score lies in ``[0, 1]`` and an identical spectrum scores ``1.0``;
* an empty or all-zero spectrum scores ``0.0``, never ``nan``;
* intensity the match cannot explain lowers the score (a single coincidental
  peak must not score like an identity — the defect that made 52 unrelated
  spectra score 1.0 against the synthetic library);
* the batch path (``score_many``) agrees with the single-pair path, because the
  null model only ever calls the batch path;
* the in-house tool score and the scorer agree, so the two implementations
  cannot drift apart silently.
"""

from __future__ import annotations

import numpy as np
import pytest

from msmcp.models.scoring import (
    EMBEDDING_SCORER_METHODS,
    ClassicalScorer,
    EmbeddingScorer,
    SpectrumScorer,
    get_scorer,
)
from msmcp.tools.similarity import _cosine_over_all_peaks, _match_peaks

Peaks = list[tuple[float, float]]


def _random_spectrum(rng: np.random.Generator, n_peaks: int) -> Peaks:
    """A plausible MS2 peak list: sorted m/z, positive intensities."""
    mz = np.sort(rng.uniform(50.0, 500.0, n_peaks))
    intensity = rng.uniform(1.0, 1e4, n_peaks)
    return [(float(m), float(i)) for m, i in zip(mz, intensity, strict=True)]


@pytest.fixture
def scorer() -> ClassicalScorer:
    return ClassicalScorer()


class TestFactory:
    def test_classical_resolves_to_the_classical_scorer(self) -> None:
        assert isinstance(get_scorer("classical"), ClassicalScorer)

    @pytest.mark.parametrize("method", EMBEDDING_SCORER_METHODS)
    def test_embedding_methods_resolve_to_an_embedding_scorer(
        self, method: str
    ) -> None:
        resolved = get_scorer(method)
        assert isinstance(resolved, EmbeddingScorer)
        assert resolved.name == method
        assert resolved.embedding_dim > 0

    def test_unknown_method_lists_every_known_name(self) -> None:
        with pytest.raises(ValueError, match="classical, dreams, lsm-ms2"):
            get_scorer("orbitrap-ai")

    def test_every_scorer_is_a_spectrum_scorer(self) -> None:
        for method in ("classical", *EMBEDDING_SCORER_METHODS):
            assert isinstance(get_scorer(method), SpectrumScorer)


class TestContract:
    def test_identical_spectra_score_one(self, scorer: ClassicalScorer) -> None:
        peaks = _random_spectrum(np.random.default_rng(1), 12)
        assert scorer.score(peaks, peaks) == pytest.approx(1.0, abs=1e-12)

    def test_single_coincidental_peak_is_not_an_identity(
        self, scorer: ClassicalScorer
    ) -> None:
        query = [(100.0, 1.0), (200.0, 2.0), (300.0, 5.0)]
        reference = [(100.0, 1.0), (400.0, 2.0), (500.0, 5.0)]
        assert scorer.score(query, reference) == pytest.approx(1.0 / 30.0, rel=1e-12)

    def test_empty_and_zero_intensity_inputs_score_zero(
        self, scorer: ClassicalScorer
    ) -> None:
        peaks = [(100.0, 5.0), (200.0, 1.0)]
        assert scorer.score([], peaks) == 0.0
        assert scorer.score(peaks, []) == 0.0
        assert scorer.score([(100.0, 0.0), (200.0, 0.0)], peaks) == 0.0
        assert scorer.score(peaks, [(100.0, 0.0)]) == 0.0
        assert scorer.score_many(peaks, [[]]) == [0.0]

    def test_score_is_bounded(self, scorer: ClassicalScorer) -> None:
        rng = np.random.default_rng(7)
        for _ in range(50):
            a = _random_spectrum(rng, int(rng.integers(1, 30)))
            b = _random_spectrum(rng, int(rng.integers(1, 30)))
            assert 0.0 <= scorer.score(a, b) <= 1.0

    def test_unexplained_intensity_lowers_the_score(
        self, scorer: ClassicalScorer
    ) -> None:
        """Adding query peaks the reference cannot explain must lower the score."""
        reference = [(100.0, 50.0), (200.0, 100.0), (300.0, 25.0)]
        diluted = [(100.0, 50.0), (200.0, 100.0), (300.0, 25.0), (900.0, 80.0)]
        assert scorer.score(reference, reference) > scorer.score(diluted, reference)

    def test_a_narrower_tolerance_cannot_raise_the_score(self) -> None:
        rng = np.random.default_rng(3)
        query = _random_spectrum(rng, 15)
        reference = _random_spectrum(rng, 15)
        wide = ClassicalScorer(0.05).score(query, reference)
        narrow = ClassicalScorer(0.001).score(query, reference)
        assert narrow <= wide

    def test_batch_agrees_with_single_pair_scoring(
        self, scorer: ClassicalScorer
    ) -> None:
        rng = np.random.default_rng(11)
        query = _random_spectrum(rng, 20)
        references = [
            _random_spectrum(rng, int(rng.integers(0, 25))) for _ in range(40)
        ]
        assert scorer.score_many(query, references) == pytest.approx(
            [scorer.score(query, reference) for reference in references]
        )

    @pytest.mark.parametrize("method", EMBEDDING_SCORER_METHODS)
    def test_embedding_batch_agrees_with_single_pair_scoring(self, method: str) -> None:
        resolved = get_scorer(method)
        rng = np.random.default_rng(5)
        query = _random_spectrum(rng, 9)
        references = [_random_spectrum(rng, int(rng.integers(1, 12))) for _ in range(6)]
        assert resolved.score_many(query, references) == pytest.approx(
            [resolved.score(query, reference) for reference in references]
        )

    @pytest.mark.parametrize(
        ("field", "value"),
        [("tolerance", 0.0), ("tolerance", -0.02)],
    )
    def test_invalid_configuration_is_refused(self, field: str, value: float) -> None:
        with pytest.raises(ValueError, match=f"{field} must be positive"):
            ClassicalScorer(**{field: value})


class TestAgreementWithTheToolScorer:
    """``tools/similarity.py`` keeps its own cosine to report matched peaks.

    The two must not drift: the tool's score and the scorer's score are the same
    quantity, and the scan and the tool are read by the same user.
    """

    def test_scores_agree_on_random_spectra(self) -> None:
        scorer = ClassicalScorer()
        rng = np.random.default_rng(23)
        for _ in range(25):
            query = np.asarray(
                _random_spectrum(rng, int(rng.integers(1, 20))), dtype=np.float64
            )
            reference = np.asarray(
                _random_spectrum(rng, int(rng.integers(1, 20))), dtype=np.float64
            )
            matched_q, matched_r, _ = _match_peaks(query, reference, 0.02)
            tool_score = _cosine_over_all_peaks(matched_q, matched_r, query, reference)
            assert scorer.score(query.tolist(), reference.tolist()) == pytest.approx(
                tool_score, abs=1e-12
            )

    def test_scores_agree_on_identical_spectra(self) -> None:
        scorer = ClassicalScorer()
        peaks = np.asarray(
            _random_spectrum(np.random.default_rng(29), 8), dtype=np.float64
        )
        matched_q, matched_r, _ = _match_peaks(peaks, peaks, 0.02)
        tool_score = _cosine_over_all_peaks(matched_q, matched_r, peaks, peaks)
        assert scorer.score(peaks.tolist(), peaks.tolist()) == pytest.approx(
            tool_score, abs=1e-12
        )
        assert tool_score == pytest.approx(1.0, abs=1e-12)
