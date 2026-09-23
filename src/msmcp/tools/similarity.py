"""Similarity & validation tools: mass-error checks and spectral matching.

Scoring backends
----------------
* ``classical`` - greedy one-to-one peak matching within a Da tolerance,
  scored with cosine similarity on the matched intensities.
* ``dreams`` / ``lsm-ms2`` - whole-spectrum embeddings produced by the real
  foundation-model adapters in :mod:`msmcp.models.backends`.  These require
  real inference; when a backend is unavailable in production mode, the tool
  raises
  :class:`~msmcp.models.backends.EmbeddingBackendUnavailable` instead of
  returning a result that looks learned.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any, Literal

import numpy as np
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from msmcp.models import EmbeddingBackendUnavailable, get_embedder

logger = logging.getLogger("msmcp.tools.similarity")


# ======================================================================
# Pydantic schemas
# ======================================================================
class ValidatePrecursorInput(BaseModel):
    """Input for the validate_precursor tool."""

    theoretical_mass: float = Field(..., gt=0.0)
    experimental_mass: float = Field(..., gt=0.0)


class ComputeCosineInput(BaseModel):
    """Input for the compute_cosine tool."""

    query_peaks: list[list[float]] = Field(..., min_length=1)
    reference_peaks: list[list[float]] = Field(..., min_length=1)
    ms2_tolerance: float = Field(default=0.02, gt=0.0, le=1.0)
    scoring_method: Literal["classical", "dreams", "lsm-ms2"] = "classical"


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
            raise ValueError(f"{label} peak [{i}] must be [m/z, intensity]; got {p!r}")
        if p[1] < 0:
            raise ValueError(f"{label} peak [{i}] has negative intensity ({p[1]})")
    arr = np.asarray(peaks, dtype=np.float64)
    return arr


def _fmt_mz(val: float) -> str:
    return f"{val:.4f}"


def _fmt_intensity(val: float) -> str:
    return f"{val:.2e}" if abs(val) >= 1e6 else f"{val:.2f}"


# ======================================================================
# Core: cosine similarity (classical / analytical implementation)
# ======================================================================
def _match_peaks(
    query: np.ndarray,  # (N, 2)  [mz, intensity]
    reference: np.ndarray,  # (M, 2)
    tolerance: float,
) -> tuple[np.ndarray, np.ndarray, list[int]]:
    """Greedy peak matching within *tolerance* Da.

    Returns
    -------
    q_intensities : (K,) float64  - intensity vector for matched query peaks
    r_intensities : (K,) float64  - intensity vector for matched ref peaks
    unmatched_q   : list[int]     - indices of query peaks with no match
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
# Core: deep-embedding scoring (foundation-model adapters)
# ======================================================================
def _embedding_score(
    query: np.ndarray,
    reference: np.ndarray,
    n_query: int,
    n_ref: int,
    method: str,
) -> str:
    """Score two peak lists in deep-embedding space and render the report."""
    embedder = get_embedder(method)
    try:
        q_emb = embedder.embed_spectrum(query)
        r_emb = embedder.embed_spectrum(reference)
    except EmbeddingBackendUnavailable:
        raise  # availability failures must not be swallowed into a string
    except Exception as exc:  # adapters must never crash the tool
        logger.warning("%s embedding failed: %s", embedder.name, exc)
        return f"ERROR: {embedder.name} embedding failed: {exc}"

    # Unit-normalise both embeddings and score with the dot product u·v.
    # The adapters already return L2-normalised vectors, but normalising
    # again keeps the dot product well-defined for any embedder.
    u = q_emb.astype(np.float64)
    v = r_emb.astype(np.float64)
    u_norm = np.linalg.norm(u)
    v_norm = np.linalg.norm(v)
    if u_norm == 0.0 or v_norm == 0.0:
        score = 0.0
    else:
        score = float(np.dot(u / u_norm, v / v_norm))

    backend_label = embedder.backend_label

    logger.info(
        "compute_cosine(method=%s, query=%d, ref=%d) → %.4f",
        method,
        n_query,
        n_ref,
        score,
    )

    return (
        f"Cosine Similarity ({embedder.name}): **{score:.4f}**\n"
        "\n"
        f"Scoring method: {embedder.name} deep embedding "
        f"({embedder.embedding_dim}-d, L2-normalised, {backend_label})\n"
        f"Query peaks: {n_query} | Reference peaks: {n_ref}\n"
        "\n"
        "Similarity is computed in embedding space: whole-spectrum\n"
        "fragmentation patterns are compared rather than individual\n"
        "peak matches, so no matched-peak counts are reported."
    )


# ======================================================================
# Public registration
# ======================================================================
def register_tools(mcp: Any) -> None:
    """Register similarity & validation tools on the MCPServer *mcp* instance."""

    # ------------------------------------------------------------------
    # Tool: validate_precursor
    # ------------------------------------------------------------------
    @mcp.tool(
        title="Validate Precursor Mass",
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False),
    )
    def validate_precursor(
        theoretical_mass: Annotated[
            float,
            Field(
                gt=0.0,
                description="Exact monoisotopic mass of the hypothesised compound (Da).",
            ),
        ],
        experimental_mass: Annotated[
            float,
            Field(
                gt=0.0,
                description="Experimentally observed precursor m/z (Da).",
            ),
        ],
    ) -> str:
        """Check whether an observed precursor mass matches a theoretical mass.

        Use this before interpreting an MS/MS spectrum to confirm that the
        precursor ion is physically consistent with the hypothesised compound,
        or to discriminate between candidate molecular formulas, adducts or
        charge states.

        Both masses must be given in daltons (Da) and must be greater than 0;
        the theoretical mass is the exact monoisotopic mass of the neutral (or
        already ionised) species being compared, and the experimental mass is
        the observed precursor m/z.

        Returns the parts-per-million (ppm) mass error together with a PASSED
        or REJECTED verdict.  The match passes only when the absolute error is
        5.0 ppm or less; a rejection means the observation is inconsistent
        with the hypothesis and the formula, adduct assignment or instrument
        calibration should be reconsidered.
        """
        _ = ValidatePrecursorInput(
            theoretical_mass=theoretical_mass,
            experimental_mass=experimental_mass,
        )

        delta_ppm = abs(theoretical_mass - experimental_mass) / theoretical_mass * 1e6
        passed = delta_ppm <= 5.0

        logger.info(
            "validate_precursor(theo=%.4f, exp=%.4f) → %.2f ppm (%s)",
            theoretical_mass,
            experimental_mass,
            delta_ppm,
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
    @mcp.tool(
        title="Compute MS/MS Cosine Similarity",
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False),
    )
    def compute_cosine(
        query_peaks: Annotated[
            list[list[float]],
            Field(
                min_length=1,
                description=(
                    "Query spectrum peaks as [[m/z, intensity], ...]; at least "
                    "one peak is required."
                ),
            ),
        ],
        reference_peaks: Annotated[
            list[list[float]],
            Field(
                min_length=1,
                description=(
                    "Reference spectrum peaks as [[m/z, intensity], ...]; at "
                    "least one peak is required."
                ),
            ),
        ],
        ms2_tolerance: Annotated[
            float,
            Field(
                gt=0.0,
                le=1.0,
                description=(
                    "m/z matching tolerance in Da (default 0.02); must be "
                    "greater than 0 and at most 1.0."
                ),
            ),
        ] = 0.02,
        scoring_method: Annotated[
            Literal["classical", "dreams", "lsm-ms2"],
            Field(
                description=(
                    "Scoring method: 'classical' performs greedy one-to-one "
                    "peak matching within ms2_tolerance, while 'dreams' and "
                    "'lsm-ms2' compare whole-spectrum embeddings from the "
                    "corresponding foundation model."
                ),
            ),
        ] = "classical",
    ) -> str:
        """Score the similarity between an experimental MS/MS spectrum and a reference spectrum.

        Use this for spectral library matching, to rank candidate reference
        spectra against an unknown query spectrum, or to judge how well a
        proposed structure explains the observed fragmentation.

        Peaks are supplied as [[m/z, intensity], ...] lists in daltons; each
        list needs at least one peak, intensities must be non-negative, and
        intensities should be on a consistent scale within each spectrum.

        Returns a cosine score between 0 and 1 (1.0 means identical), the
        number of matched peaks and the percentage of query peaks matched, and
        a list of the most intense unmatched query peaks, which point at
        structural differences to investigate.

        With scoring_method='classical' (the default) query peaks are matched
        to the closest unused reference peak within ms2_tolerance Da (greedy,
        one-to-one), so the tolerance must be greater than 0 and at most 1.0.
        With 'dreams' or 'lsm-ms2' the comparison happens in whole-spectrum
        embedding space instead and no peak-matching counts are reported;
        those methods need the corresponding foundation model to be installed,
        otherwise the tool returns an error rather than a score.
        """
        _ = ComputeCosineInput(
            query_peaks=query_peaks,
            reference_peaks=reference_peaks,
            ms2_tolerance=ms2_tolerance,
            scoring_method=scoring_method,
        )

        # --- validate & convert peak lists ----------------------------------
        try:
            q_arr = _validate_peak_list(query_peaks, "Query")
            r_arr = _validate_peak_list(reference_peaks, "Reference")
        except ValueError as exc:
            logger.warning("Peak list validation failed: %s", exc)
            return f"ERROR: {exc}"

        n_query = len(q_arr)
        n_ref = len(r_arr)

        # --- foundation-model embedding scoring -----------------------------
        if scoring_method != "classical":
            return _embedding_score(q_arr, r_arr, n_query, n_ref, scoring_method)

        # --- classical greedy matching --------------------------------------
        q_matched, r_matched, unmatched_q_idx = _match_peaks(
            q_arr,
            r_arr,
            ms2_tolerance,
        )

        # --- cosine ---------------------------------------------------------
        score = _cosine(q_matched, r_matched)

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
            unmatched_lines.append(f"  {'m/z':>10}  {'Intensity':>12}")
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
            "Scoring method: classical (greedy peak matching)",
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
            n_query,
            n_ref,
            ms2_tolerance,
            score,
            n_matched,
            len(unmatched_q_idx),
        )

        return "\n".join(lines)
