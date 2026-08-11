"""Similarity &amp; validation tools: mass-error checks and spectral matching."""

from __future__ import annotations

import logging
import math
from typing import Any

import numpy as np
from pydantic import BaseModel, Field

logger = logging.getLogger("msmcp.tools.similarity")

# ======================================================================
# Pydantic schemas
# ======================================================================
class ValidatePrecursorInput(BaseModel):
    """Input for the validate_precursor tool."""

    theoretical_mass: float = Field(
        ...,
        gt=0.0,
        description="Exact monoisotopic mass of the hypothesised compound (Da).",
    )
    experimental_mass: float = Field(
        ...,
        gt=0.0,
        description="Experimentally observed precursor m/z (Da).",
    )


class ComputeCosineInput(BaseModel):
    """Input for the compute_cosine tool."""

    query_peaks: list[list[float]] = Field(
        ...,
        min_length=1,
        description="Query spectrum peaks as [[m/z, intensity], ...].",
    )
    reference_peaks: list[list[float]] = Field(
        ...,
        min_length=1,
        description="Reference spectrum peaks as [[m/z, intensity], ...].",
    )
    ms2_tolerance: float = Field(
        default=0.02,
        gt=0.0,
        le=1.0,
        description="m/z matching tolerance in Da (default 0.02).",
    )


# ======================================================================
# Helpers
# ======================================================================
def _validate_peak_list(
    peaks: list[list[float]],
    label: str,
) -> np.ndarray:
    """Convert a raw peak list into a float64 (N, 2) array, validating shape."""
    if not peaks:
        raise ValueError(f"{label} peak list must be non-empty.")
    for i, p in enumerate(peaks):
        if not isinstance(p, (list, tuple)) or len(p) != 2:
            raise ValueError(
                f"{label} peak [{i}] must be [m/z, intensity]; got {p!r}"
            )
        if p[1] < 0:
            raise ValueError(
                f"{label} peak [{i}] has negative intensity ({p[1]})"
            )
    arr = np.asarray(peaks, dtype=np.float64)
    return arr


def _fmt_mz(val: float) -> str:
    return f"{val:.4f}"


def _fmt_intensity(val: float) -> str:
    return f"{val:.2e}" if abs(val) >= 1e6 else f"{val:.2f}"


# ======================================================================
# Core: cosine similarity (mock / analytical implementation)
# ======================================================================
def _match_peaks(
    query: np.ndarray,       # (N, 2)  [mz, intensity]
    reference: np.ndarray,   # (M, 2)
    tolerance: float,
) -> tuple[np.ndarray, np.ndarray, list[int]]:
    """Greedy peak matching within *tolerance* Da.

    Returns
    -------
    q_intensities : (K,) float64  – intensity vector for matched query peaks
    r_intensities : (K,) float64  – intensity vector for matched ref peaks
    unmatched_q   : list[int]     – indices of query peaks with no match
    """
    # Sort reference by m/z for binary-search acceleration
    ref_order = np.argsort(reference[:, 0])
    ref_sorted = reference[ref_order]

    matched_q_int: list[float] = []
    matched_r_int: list[float] = []
    unmatched_q: list[int] = []

    # Track which reference peaks have been consumed (greedy, one-to-one)
    ref_used = np.zeros(len(reference), dtype=bool)

    for qi, (qmz, qint) in enumerate(query):
        # Find reference peaks within tolerance
        lo = np.searchsorted(ref_sorted[:, 0], qmz - tolerance, side="left")
        hi = np.searchsorted(ref_sorted[:, 0], qmz + tolerance, side="right")

        if lo >= hi:
            unmatched_q.append(qi)
            continue

        # Choose the closest m/z among candidates not yet used
        candidates = ref_sorted[lo:hi]
        candidate_indices = ref_order[lo:hi]

        best_offset = float("inf")
        best_idx = -1
        best_rint = 0.0

        for j in range(len(candidates)):
            global_idx = candidate_indices[j]
            if ref_used[global_idx]:
                continue
            offset = abs(candidates[j, 0] - qmz)
            if offset < best_offset:
                best_offset = offset
                best_idx = global_idx
                best_rint = candidates[j, 1]

        if best_idx < 0:
            unmatched_q.append(qi)
        else:
            ref_used[best_idx] = True
            matched_q_int.append(qint)
            matched_r_int.append(best_rint)

    q_vec = np.array(matched_q_int, dtype=np.float64)
    r_vec = np.array(matched_r_int, dtype=np.float64)
    return q_vec, r_vec, unmatched_q


def _cosine(a: np.ndarray, b: np.ndarray) -> float:
    """Cosine similarity between two non-negative vectors."""
    if len(a) == 0:
        return 0.0
    dot = np.dot(a, b)
    norm_a = np.linalg.norm(a)
    norm_b = np.linalg.norm(b)
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return float(dot / (norm_a * norm_b))


# ======================================================================
# Public registration
# ======================================================================
def register_tools(mcp: Any) -> None:
    """Register similarity &amp; validation tools on the FastMCP *mcp* instance."""

    # ------------------------------------------------------------------
    # Tool: validate_precursor
    # ------------------------------------------------------------------
    @mcp.tool()
    def validate_precursor(theoretical_mass: float, experimental_mass: float) -> str:
        """Validate an experimental precursor mass against a theoretical mass.

        Computes the parts-per-million mass error.  If the error exceeds
        5.0 ppm the match is rejected — the observed spectrum is
        physically inconsistent with the hypothesised compound.
        """
        _ = ValidatePrecursorInput(
            theoretical_mass=theoretical_mass,
            experimental_mass=experimental_mass,
        )

        delta_ppm = abs(theoretical_mass - experimental_mass) / theoretical_mass * 1e6
        passed = delta_ppm <= 5.0

        logger.info(
            "validate_precursor(theo=%.4f, exp=%.4f) → %.2f ppm (%s)",
            theoretical_mass, experimental_mass, delta_ppm,
            "PASS" if passed else "REJECT",
        )

        if passed:
            return (
                f"VALIDATION PASSED\n"
                f"Theoretical mass:  {theoretical_mass:.6f} Da\n"
                f"Experimental mass:  {experimental_mass:.6f} Da\n"
                f"Mass error:         {delta_ppm:.2f} ppm\n\n"
                f"The observed precursor is consistent with the hypothesised "
                f"compound (≤ 5.0 ppm threshold)."
            )
        else:
            return (
                f"VALIDATION REJECTED\n"
                f"Theoretical mass:  {theoretical_mass:.6f} Da\n"
                f"Experimental mass:  {experimental_mass:.6f} Da\n"
                f"Mass error:         {delta_ppm:.2f} ppm\n\n"
                f"The mass error exceeds the 5.0 ppm acceptance threshold. "
                f"The observed spectrum is **physically invalid** for the "
                f"hypothesised compound.  Reconsider the molecular formula, "
                f"adduct assignment, or instrument calibration."
            )

    # ------------------------------------------------------------------
    # Tool: compute_cosine
    # ------------------------------------------------------------------
    @mcp.tool()
    def compute_cosine(
        query_peaks: list[list[float]],
        reference_peaks: list[list[float]],
        ms2_tolerance: float = 0.02,
    ) -> str:
        """Compute the cosine similarity between two MS/MS peak lists.

        Matches query peaks to the closest reference peak within
        *ms2_tolerance* Da (greedy, one-to-one).  Reports the cosine
        score, match counts, and the most intense *unmatched* query
        peaks to guide structural revision.
        """
        _ = ComputeCosineInput(
            query_peaks=query_peaks,
            reference_peaks=reference_peaks,
            ms2_tolerance=ms2_tolerance,
        )

        # --- validate & convert peak lists ----------------------------------
        try:
            q_arr = _validate_peak_list(query_peaks, "Query")
            r_arr = _validate_peak_list(reference_peaks, "Reference")
        except ValueError as exc:
            logger.warning("Peak list validation failed: %s", exc)
            return f"ERROR: {exc}"

        # --- match ----------------------------------------------------------
        q_matched, r_matched, unmatched_q_idx = _match_peaks(
            q_arr, r_arr, ms2_tolerance,
        )

        # --- cosine ---------------------------------------------------------
        score = _cosine(q_matched, r_matched)

        n_query = len(q_arr)
        n_ref = len(r_arr)
        n_matched = len(q_matched)
        pct_matched = (n_matched / n_query * 100) if n_query > 0 else 0.0

        # --- unmatched query peaks (sorted by intensity, descending) --------
        unmatched_lines: list[str] = []
        if unmatched_q_idx:
            # Sort unmatched indices by intensity descending
            order = sorted(unmatched_q_idx, key=lambda i: q_arr[i, 1], reverse=True)
            # Show up to 15 most intense unmatched peaks
            unmatched_lines.append(
                "Unmatched query peaks (most intense first; these fragments may indicate"
                " structural differences):"
            )
            unmatched_lines.append(
                f"  {'m/z':>10}  {'Intensity':>12}"
            )
            unmatched_lines.append(f"  {'─' * 10}  {'─' * 12}")
            for i in order[:15]:
                unmatched_lines.append(
                    f"  {_fmt_mz(q_arr[i, 0]):>10}  {_fmt_intensity(q_arr[i, 1]):>12}"
                )
            if len(order) > 15:
                unmatched_lines.append(
                    f"  ... and {len(order) - 15} more unmatched peaks"
                )

        # --- used ref peaks -------------------------------------------------
        n_ref_used = n_matched  # one-to-one matching
        pct_ref_used = (n_ref_used / n_ref * 100) if n_ref > 0 else 0.0

        # --- assemble output ------------------------------------------------
        lines = [
            f"Cosine Similarity: **{score:.4f}**",
            "",
            f"Matched: {n_matched} / {n_query} query peaks ({pct_matched:.1f}%)",
            f"Reference peaks utilised: {n_ref_used} / {n_ref} ({pct_ref_used:.1f}%)",
            f"MS/MS tolerance: ±{ms2_tolerance:.3f} Da",
            "",
        ]

        if unmatched_lines:
            lines.extend(unmatched_lines)
        else:
            lines.append("All query peaks were matched to the reference spectrum.")

        logger.info(
            "compute_cosine(query=%d, ref=%d, tol=%.3f) → %.4f (%d matched, %d unmatched)",
            n_query, n_ref, ms2_tolerance, score, n_matched, len(unmatched_q_idx),
        )

        return "\n".join(lines)
