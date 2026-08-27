"""Tests for the similarity & validation tools.

Covers ``validate_precursor`` (ppm mass-error math, boundary conditions
around the 5.0 ppm threshold), the greedy peak-matching logic behind
``_cosine``, and the deep-embedding scoring methods (DreaMS / LSM-MS2)
provided by the foundation-model adapters.
"""

from __future__ import annotations

from collections.abc import Callable

import numpy as np
import pytest
from pydantic import ValidationError

from msmcp.tools.similarity import _cosine, _match_peaks


def _arr(*peaks: tuple[float, float]) -> np.ndarray:
    """Build an (N, 2) float64 peak array from (m/z, intensity) pairs."""
    return np.asarray(peaks, dtype=np.float64)


# ---------------------------------------------------------------------------
# validate_precursor — ppm math and the 5.0 ppm boundary
# ---------------------------------------------------------------------------
class TestValidatePrecursor:
    """Boundary values chosen so the arithmetic is exact in float64:
    |1e6 - 999995| / 1e6 * 1e6 == 5.0 exactly."""

    def test_zero_error_passes(self, sim_tools: dict[str, Callable[..., str]]) -> None:
        out = sim_tools["validate_precursor"](
            theoretical_mass=123.456, experimental_mass=123.456
        )
        assert out.startswith("VALIDATION PASSED")
        assert "Mass error:         0.00 ppm" in out

    @pytest.mark.parametrize(
        ("theoretical", "experimental"),
        [
            (1_000_000.0, 999_995.0),  # exactly -5.0 ppm (lower bound, inclusive)
            (1_000_000.0, 1_000_005.0),  # exactly +5.0 ppm (upper bound, inclusive)
        ],
    )
    def test_boundary_exactly_5ppm_passes(
        self,
        sim_tools: dict[str, Callable[..., str]],
        theoretical: float,
        experimental: float,
    ) -> None:
        out = sim_tools["validate_precursor"](
            theoretical_mass=theoretical, experimental_mass=experimental
        )
        assert out.startswith("VALIDATION PASSED")
        assert "Mass error:         5.00 ppm" in out
        assert "consistent with the hypothesised compound" in out

    @pytest.mark.parametrize(
        ("theoretical", "experimental"),
        [
            (1_000_000.0, 999_994.999),  # 5.001 ppm → just outside
            (1_000_000.0, 1_000_005.001),  # 5.001 ppm → just outside
        ],
    )
    def test_just_outside_5ppm_is_rejected(
        self,
        sim_tools: dict[str, Callable[..., str]],
        theoretical: float,
        experimental: float,
    ) -> None:
        out = sim_tools["validate_precursor"](
            theoretical_mass=theoretical, experimental_mass=experimental
        )
        assert out.startswith("VALIDATION REJECTED")
        assert "exceeds the 5.0 ppm acceptance threshold" in out

    def test_well_below_5ppm_passes(
        self, sim_tools: dict[str, Callable[..., str]]
    ) -> None:
        out = sim_tools["validate_precursor"](
            theoretical_mass=100.0,
            experimental_mass=100.0004,  # 4.0 ppm
        )
        assert out.startswith("VALIDATION PASSED")
        assert "Mass error:         4.00 ppm" in out

    def test_well_above_5ppm_is_rejected(
        self, sim_tools: dict[str, Callable[..., str]]
    ) -> None:
        out = sim_tools["validate_precursor"](
            theoretical_mass=100.0,
            experimental_mass=100.0006,  # 6.0 ppm
        )
        assert out.startswith("VALIDATION REJECTED")
        assert "Mass error:         6.00 ppm" in out

    @pytest.mark.parametrize(
        ("theoretical", "experimental"),
        [(0.0, 100.0), (-1.0, 100.0), (100.0, 0.0), (100.0, -5.0)],
    )
    def test_non_positive_masses_raise_validation_error(
        self,
        sim_tools: dict[str, Callable[..., str]],
        theoretical: float,
        experimental: float,
    ) -> None:
        with pytest.raises(ValidationError):
            sim_tools["validate_precursor"](
                theoretical_mass=theoretical, experimental_mass=experimental
            )


# ---------------------------------------------------------------------------
# _match_peaks — greedy one-to-one matching within tolerance
# ---------------------------------------------------------------------------
class TestMatchPeaks:
    def test_perfect_match(self) -> None:
        query = _arr((100.0, 1.0), (200.0, 2.0))
        reference = _arr((100.0, 1.0), (200.0, 2.0))
        q_matched, r_matched, unmatched = _match_peaks(query, reference, 0.02)
        assert unmatched == []
        assert q_matched.tolist() == [1.0, 2.0]
        assert r_matched.tolist() == [1.0, 2.0]
        assert _cosine(q_matched, r_matched) == pytest.approx(1.0)

    def test_tolerance_boundary_is_inclusive(self) -> None:
        """A query peak exactly ±tolerance Da away must still match."""
        reference = _arr((100.0, 9.0))
        q_hi, _, u_hi = _match_peaks(_arr((101.0, 5.0)), reference, 1.0)
        q_lo, _, u_lo = _match_peaks(_arr((99.0, 5.0)), reference, 1.0)
        assert u_hi == [] and u_lo == []
        assert q_hi.tolist() == [5.0] and q_lo.tolist() == [5.0]

    @pytest.mark.parametrize("mz", [101.1, 98.899999])
    def test_just_outside_tolerance_is_unmatched(self, mz: float) -> None:
        _, _, unmatched = _match_peaks(_arr((mz, 5.0)), _arr((100.0, 9.0)), 1.0)
        assert unmatched == [0]

    def test_greedy_one_to_one_consumes_reference(self) -> None:
        """Two query peaks, one reference: only the first can match."""
        q_matched, r_matched, unmatched = _match_peaks(
            _arr((100.01, 5.0), (100.02, 6.0)), _arr((100.0, 10.0)), 0.1
        )
        assert unmatched == [1]
        assert q_matched.tolist() == [5.0]
        assert r_matched.tolist() == [10.0]

    def test_closest_candidate_wins(self) -> None:
        """Among unused references, the nearest m/z is selected."""
        _, r_matched, unmatched = _match_peaks(
            _arr((100.0, 1.0)), _arr((99.9, 2.0), (100.05, 3.0)), 0.2
        )
        assert unmatched == []
        assert r_matched.tolist() == [3.0]  # 100.05 is 0.05 Da; 99.9 is 0.1 Da

    def test_consumed_reference_is_not_reused(self) -> None:
        """Second query peak must fall through to the next-closest ref."""
        _, r_matched, unmatched = _match_peaks(
            _arr((100.0, 1.0), (100.0, 2.0)),
            _arr((100.0, 10.0), (99.95, 20.0)),
            0.1,
        )
        assert unmatched == []
        assert r_matched.tolist() == [10.0, 20.0]

    def test_no_reference_within_tolerance(self) -> None:
        q_matched, r_matched, unmatched = _match_peaks(
            _arr((150.0, 1.0), (160.0, 2.0)), _arr((100.0, 9.0)), 0.02
        )
        assert unmatched == [0, 1]
        assert len(q_matched) == 0 and len(r_matched) == 0
        assert _cosine(q_matched, r_matched) == 0.0

    def test_matched_vectors_follow_query_order(self) -> None:
        query = _arr((200.0, 2.0), (100.0, 1.0))
        reference = _arr((100.0, 1.0), (200.0, 2.0))
        q_matched, r_matched, unmatched = _match_peaks(query, reference, 0.02)
        assert unmatched == []
        assert q_matched.tolist() == [2.0, 1.0]  # query order preserved
        assert r_matched.tolist() == [2.0, 1.0]  # intensities pair up correctly


# ---------------------------------------------------------------------------
# _cosine — vector math
# ---------------------------------------------------------------------------
class TestCosine:
    def test_empty_vectors(self) -> None:
        assert _cosine(np.array([]), np.array([])) == 0.0

    def test_zero_norm_returns_zero(self) -> None:
        assert _cosine(np.array([0.0, 0.0]), np.array([1.0, 1.0])) == 0.0

    def test_identical_vectors(self) -> None:
        a = np.array([1.0, 2.0, 3.0])
        assert _cosine(a, a) == pytest.approx(1.0)

    def test_orthogonal_vectors(self) -> None:
        assert _cosine(np.array([1.0, 0.0]), np.array([0.0, 1.0])) == pytest.approx(
            0.0, abs=1e-12
        )

    def test_known_value_3d(self) -> None:
        # 32 / (√14 · √77)
        assert _cosine(np.array([1.0, 2.0, 3.0]), np.array([4.0, 5.0, 6.0])) == (
            pytest.approx(0.9746318462, abs=1e-9)
        )

    def test_45_degree_vectors(self) -> None:
        assert _cosine(np.array([1.0, 0.0]), np.array([1.0, 1.0])) == pytest.approx(
            0.7071067812, abs=1e-9
        )

    def test_scale_invariant(self) -> None:
        assert _cosine(np.array([1.0, 2.0]), np.array([2.0, 4.0])) == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# compute_cosine — end-to-end
# ---------------------------------------------------------------------------
class TestComputeCosine:
    def test_identical_spectra(self, sim_tools: dict[str, Callable[..., str]]) -> None:
        peaks = [[100.0, 50.0], [200.0, 100.0], [300.0, 25.0]]
        out = sim_tools["compute_cosine"](query_peaks=peaks, reference_peaks=peaks)
        assert "Cosine Similarity: **1.0000**" in out
        assert "Matched: 3 / 3 query peaks (100.0%)" in out
        assert "All query peaks were matched to the reference spectrum." in out

    def test_partial_match_reports_unmatched_peaks(
        self, sim_tools: dict[str, Callable[..., str]]
    ) -> None:
        query = [[100.0, 50.0], [200.0, 100.0], [999.0, 10.0]]
        reference = [[100.0, 50.0], [200.0, 100.0]]
        out = sim_tools["compute_cosine"](query_peaks=query, reference_peaks=reference)
        assert "Matched: 2 / 3 query peaks (66.7%)" in out
        assert "Unmatched query peaks" in out
        assert "999.0000" in out

    def test_negative_intensity_returns_error(
        self, sim_tools: dict[str, Callable[..., str]]
    ) -> None:
        out = sim_tools["compute_cosine"](
            query_peaks=[[100.0, -1.0]], reference_peaks=[[100.0, 1.0]]
        )
        assert out.startswith("ERROR: Query peak [0] has negative intensity (-1.0)")

    def test_empty_peak_list_raises_validation_error(
        self, sim_tools: dict[str, Callable[..., str]]
    ) -> None:
        with pytest.raises(ValidationError):
            sim_tools["compute_cosine"](query_peaks=[], reference_peaks=[[100.0, 1.0]])

    def test_non_positive_tolerance_raises_validation_error(
        self, sim_tools: dict[str, Callable[..., str]]
    ) -> None:
        with pytest.raises(ValidationError):
            sim_tools["compute_cosine"](
                query_peaks=[[100.0, 1.0]],
                reference_peaks=[[100.0, 1.0]],
                ms2_tolerance=0.0,
            )


# ---------------------------------------------------------------------------
# compute_cosine — foundation-model embedding scoring
# ---------------------------------------------------------------------------
class TestComputeCosineEmbeddings:
    def test_dreams_embedding_scoring(
        self, sim_tools: dict[str, Callable[..., str]]
    ) -> None:
        peaks = [[110.0713, 40.0], [120.0808, 100.0], [136.0757, 60.0]]
        out = sim_tools["compute_cosine"](
            query_peaks=peaks, reference_peaks=peaks, scoring_method="dreams"
        )
        assert "Cosine Similarity (DreaMS): **1.0000**" in out
        assert (
            "Scoring method: DreaMS deep embedding "
            "(1024-d, L2-normalised, deterministic fallback)"
        ) in out
        assert "Matched:" not in out  # no per-peak counts in embedding space

    def test_lsm_ms2_embedding_scoring(
        self, sim_tools: dict[str, Callable[..., str]]
    ) -> None:
        peaks = [[110.0713, 40.0], [120.0808, 100.0], [136.0757, 60.0]]
        out = sim_tools["compute_cosine"](
            query_peaks=peaks, reference_peaks=peaks, scoring_method="lsm-ms2"
        )
        assert "Cosine Similarity (LSM-MS2): **1.0000**" in out
        assert (
            "LSM-MS2 deep embedding (1024-d, L2-normalised, deterministic fallback)"
        ) in out

    def test_disjoint_spectra_score_zero_in_embedding_space(
        self, sim_tools: dict[str, Callable[..., str]]
    ) -> None:
        out = sim_tools["compute_cosine"](
            query_peaks=[[100.0, 50.0]],
            reference_peaks=[[900.0, 50.0]],
            scoring_method="dreams",
        )
        assert "**0.0000**" in out

    def test_partial_overlap_scores_between_zero_and_one(
        self, sim_tools: dict[str, Callable[..., str]]
    ) -> None:
        shared = [[110.0713, 40.0], [120.0808, 100.0]]
        superset = [[110.0713, 40.0], [120.0808, 100.0], [500.1, 55.0], [700.2, 45.0]]
        out = sim_tools["compute_cosine"](
            query_peaks=shared, reference_peaks=superset, scoring_method="lsm-ms2"
        )
        header = out.splitlines()[0]
        score = float(header.split("**")[1])
        assert 0.0 < score < 1.0

    def test_classical_output_states_method(
        self, sim_tools: dict[str, Callable[..., str]]
    ) -> None:
        peaks = [[100.0, 50.0], [200.0, 100.0]]
        out = sim_tools["compute_cosine"](query_peaks=peaks, reference_peaks=peaks)
        assert "Scoring method: classical (greedy peak matching)" in out

    @pytest.mark.parametrize("bad", ["specter2", "cosine", "DreaMS", ""])
    def test_invalid_scoring_method_raises_validation_error(
        self, sim_tools: dict[str, Callable[..., str]], bad: str
    ) -> None:
        with pytest.raises(ValidationError):
            sim_tools["compute_cosine"](
                query_peaks=[[100.0, 1.0]],
                reference_peaks=[[100.0, 1.0]],
                scoring_method=bad,
            )
