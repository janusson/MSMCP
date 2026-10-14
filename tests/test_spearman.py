"""Regression tests for the tie-aware Spearman used by the similarity validation.

`validation/similarity_validation.py` reports a Spearman correlation between
MSMCP's and matchms's scores.  Spectral-similarity scores are frequently *tied*
— most pairs have no shared peaks and score exactly 0.0 — and the rank
convention matters: assigning tied values arbitrary distinct ranks (the
``argsort(argsort)`` idiom) inflates the statistic.  These tests pin the correct
tie-aware behaviour, cross-check it against ``scipy.stats.spearmanr``, and show
that the naive idiom would have overstated the same data.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import numpy as np
import pytest

_TIE_ZEROS_X = [0.0, 0.0, 0.0, 0.1, 0.5, 0.5, 0.9]
_TIE_ZEROS_Y = [0.0, 0.0, 0.1, 0.1, 0.5, 0.9, 0.9]
# scipy.stats.spearmanr on the pair above (average ranks).
_TIE_ZEROS_RHO = 0.9136304946339742


def _spearman_correlation():
    path = (
        Path(__file__).resolve().parents[1] / "validation" / "similarity_validation.py"
    )
    spec = importlib.util.spec_from_file_location("_sim_validation", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses need the module registered
    spec.loader.exec_module(module)
    return module.spearman_correlation


def _naive_spearman(x: np.ndarray, y: np.ndarray) -> float:
    """The old, tie-ignoring idiom, kept only to show it overstates."""
    rx = np.argsort(np.argsort(x)).astype(float)
    ry = np.argsort(np.argsort(y)).astype(float)
    rx -= rx.mean()
    ry -= ry.mean()
    return float((rx * ry).sum() / np.sqrt((rx**2).sum() * (ry**2).sum()))


class TestTieAwareSpearman:
    def test_matches_average_rank_reference_on_tied_data(self) -> None:
        rho = _spearman_correlation()(
            np.asarray(_TIE_ZEROS_X), np.asarray(_TIE_ZEROS_Y)
        )
        assert rho == pytest.approx(_TIE_ZEROS_RHO, abs=1e-9)

    def test_the_naive_idiom_would_have_overstated_it(self) -> None:
        """Documents the defect: distinct ranks turn 0.914 into a perfect 1.0."""
        rho = _spearman_correlation()(
            np.asarray(_TIE_ZEROS_X), np.asarray(_TIE_ZEROS_Y)
        )
        naive = _naive_spearman(np.asarray(_TIE_ZEROS_X), np.asarray(_TIE_ZEROS_Y))
        assert naive == pytest.approx(1.0)
        assert naive - rho > 0.05  # materially inflated, not a rounding artefact

    def test_repeated_zeros_are_handled(self) -> None:
        """Many exact-zero scores (the common case) must rank as ties."""
        x = np.array([0.0, 0.0, 0.0, 0.0, 0.2, 0.3, 0.9])
        y = np.array([0.0, 0.0, 0.0, 0.1, 0.1, 0.4, 0.9])
        rho = _spearman_correlation()(x, y)
        assert 0.0 < rho < 1.0

    def test_perfect_monotonic_without_ties_is_one(self) -> None:
        rho = _spearman_correlation()(
            np.array([1.0, 2.0, 3.0, 4.0, 5.0]),
            np.array([2.0, 4.0, 6.0, 8.0, 10.0]),
        )
        assert rho == pytest.approx(1.0)

    def test_constant_input_is_undefined_and_reported_as_zero(self) -> None:
        rho = _spearman_correlation()(
            np.array([0.0, 0.0, 1.0, 1.0, 2.0]),
            np.array([0.5, 0.5, 0.5, 0.5, 0.5]),
        )
        assert rho == 0.0  # not a misleading 1.0

    def test_rejects_mismatched_or_too_short_input(self) -> None:
        spearman = _spearman_correlation()
        with pytest.raises(ValueError):
            spearman(np.array([1.0, 2.0]), np.array([1.0]))
        with pytest.raises(ValueError):
            spearman(np.array([1.0]), np.array([1.0]))

    def test_agrees_with_scipy_on_tied_data(self) -> None:
        """Independent verification against an established implementation."""
        scipy_stats = pytest.importorskip("scipy.stats")
        spearman = _spearman_correlation()
        cases = [
            (_TIE_ZEROS_X, _TIE_ZEROS_Y),
            ([0.0, 0.0, 0.0, 0.0, 0.2, 0.3, 0.9], [0.0, 0.0, 0.0, 0.1, 0.1, 0.4, 0.9]),
            ([1.0, 2.0, 3.0, 4.0, 5.0], [2.0, 4.0, 6.0, 8.0, 10.0]),
        ]
        for x, y in cases:
            expected = float(scipy_stats.spearmanr(x, y).statistic)
            assert spearman(np.asarray(x), np.asarray(y)) == pytest.approx(
                expected, abs=1e-9
            )
