"""Tests for the fitted upper-tail null model (roadmap #12).

The property under test is the one the sampled null could not provide: a
q-value below the empirical floor ``1 / (n_null + 1)`` must be expressible, so a
large library can still report a strong match.
"""

from __future__ import annotations

from itertools import pairwise

import numpy as np
import pytest

from msmcp.models.null_model import (
    P_FLOOR,
    compare_to_sampled,
    fit_upper_tail,
)


def _decoy_sample(seed: int = 0, n: int = 10_000) -> np.ndarray:
    """A right-skewed decoy-like sample in [0, 1]: a zero spike and a tail."""
    rng = np.random.default_rng(seed)
    zeros = np.zeros(n // 2)
    tail = rng.beta(2.0, 5.0, size=n - zeros.size)
    return np.concatenate([zeros, tail])


class TestFit:
    def test_fit_reports_a_usable_tail(self) -> None:
        fit = fit_upper_tail(_decoy_sample())
        assert fit.n == 10_000
        assert fit.n_exceed >= 50
        assert fit.sigma > 0.0
        assert -0.9 < fit.xi < 0.5

    def test_too_few_scores_is_refused(self) -> None:
        with pytest.raises(ValueError, match="at least two"):
            fit_upper_tail([0.5])

    def test_empty_sample_is_refused(self) -> None:
        with pytest.raises(ValueError, match="at least two"):
            fit_upper_tail([])

    def test_degenerate_sample_falls_back_to_exponential(self) -> None:
        """A sample with no spread beyond the threshold cannot be a GPD."""
        fit = fit_upper_tail([0.5] * 100)
        assert "exponential" in fit.model
        assert fit.xi == 0.0
        assert fit.sigma > 0.0

    def test_survival_is_nonincreasing(self) -> None:
        fit = fit_upper_tail(_decoy_sample())
        previous = 1.0
        for excess in np.linspace(0.0, 0.5, 50):
            value = fit.survival(float(excess))
            assert value <= previous + 1e-12
            previous = value

    def test_describe_names_the_model(self) -> None:
        fit = fit_upper_tail(_decoy_sample())
        assert "generalized Pareto" in fit.describe()
        assert "exceedances" in fit.describe()


class TestPValues:
    def test_p_is_nonincreasing_in_score(self) -> None:
        fit = fit_upper_tail(_decoy_sample())
        scores = [float(s) for s in np.linspace(0.0, 1.0, 100)]
        p_values = fit.p_values(scores)
        assert all(b <= a + 1e-12 for a, b in pairwise(p_values))

    def test_a_perfect_match_beats_the_sampled_floor(self) -> None:
        """The point of #12: a q below ``1 / (n_null + 1)`` must be expressible.

        The sampled null could never report a p-value below ``1/(n+1)``, so with
        a Benjamini-Hochberg factor of ``m`` no strong match could pass.  The
        fitted tail must go below that floor.
        """
        sample = _decoy_sample()
        fit = fit_upper_tail(sample)
        sampled_floor = 1.0 / (len(sample) + 1)
        assert fit.p_value(1.0) < sampled_floor

    def test_p_never_reaches_zero(self) -> None:
        fit = fit_upper_tail(_decoy_sample())
        assert fit.p_value(1.0) >= P_FLOOR

    def test_empirical_body_matches_the_tie_convention(self) -> None:
        """At or below the threshold the p-value is the inclusive empirical one."""
        fit = fit_upper_tail(_decoy_sample())
        score = 0.0  # below the threshold; every decoy ties here
        assert fit.p_value(score) == pytest.approx(1.0)

    def test_smallest_attainable_q_uses_the_bh_factor(self) -> None:
        fit = fit_upper_tail(_decoy_sample())
        assert fit.smallest_attainable_q(2000) == pytest.approx(
            min(1.0, fit.smallest_attainable_p() * 2000)
        )
        assert fit.smallest_attainable_q(0) <= 1.0


class TestValidationAgainstSampled:
    def test_fitted_and_sampled_agree_on_the_sample_they_came_from(self) -> None:
        """Measured, not asserted: the fit must track the decoy sample it used."""
        sample = _decoy_sample(seed=3)
        fit = fit_upper_tail(sample)
        comparison = compare_to_sampled(fit, sample)
        assert comparison.n_points > 0
        assert comparison.max_abs_diff < 0.01
        assert "max |" in comparison.describe()

    def test_an_empty_sample_is_refused(self) -> None:
        fit = fit_upper_tail(_decoy_sample())
        with pytest.raises(ValueError, match="empty"):
            compare_to_sampled(fit, [])


def _library_decoy_sample(
    n_spectra: int = 500, seed: int = 7, n_decoys: int = 4000, query_id: int = 1
) -> list[float]:
    """Score a decoy sample drawn from a real (synthetic) spectral library.

    This is the sample the fitted tail is actually used on - permuted intensities
    scored against a genuine library spectrum - rather than a convenience
    distribution, so the validation test measures the approximation where it
    matters.
    """
    import random

    from msmcp.tools.search import (
        _build_mock_database,
        _build_scorer,
        _decoy_spectrum,
        _permutable,
    )

    conn = _build_mock_database(n_spectra=n_spectra, seed=seed)
    try:
        rows = conn.execute(
            "SELECT mz, intensity FROM peaks WHERE spectrum_id=? ORDER BY mz",
            (query_id,),
        ).fetchall()
        query = [(float(mz), float(intensity)) for mz, intensity in rows]
        pool: list[list[tuple[float, float]]] = []
        for (spectrum_id,) in conn.execute("SELECT id FROM spectra"):
            peaks = [
                (float(mz), float(intensity))
                for mz, intensity in conn.execute(
                    "SELECT mz, intensity FROM peaks WHERE spectrum_id=? ORDER BY mz",
                    (spectrum_id,),
                ).fetchall()
            ]
            if _permutable(peaks):
                pool.append(peaks)
    finally:
        conn.close()

    scorer = _build_scorer("classical", 0.02)
    rng = random.Random(99)
    decoys = [
        _decoy_spectrum(pool[rng.randrange(len(pool))], rng) for _ in range(n_decoys)
    ]
    return [float(score) for score in scorer.score_many(query, decoys)]


class TestLibraryValidation:
    def test_the_fit_tracks_a_real_decoy_sample(self) -> None:
        """The fitted tail must track the decoy sample it was drawn from.

        The disagreement is *measured*.  The bounds are deliberately loose
        enough to be robust across samples while still failing if the fit drifts:
        a GPD that no longer resembles its own decoys is what this catches.
        """
        null = _library_decoy_sample()
        fit = fit_upper_tail(null)
        comparison = compare_to_sampled(fit, null)
        assert comparison.n_points > 0
        assert comparison.max_abs_diff < 0.10
        assert comparison.mean_abs_diff < 0.02

    def test_a_true_positive_beats_the_sampled_floor_on_a_library(self) -> None:
        """A perfect match against a real decoy sample scores below its floor."""
        null = _library_decoy_sample()
        fit = fit_upper_tail(null)
        sampled_floor = 1.0 / (len(null) + 1)
        assert fit.p_value(1.0) < sampled_floor
