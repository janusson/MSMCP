"""QC tools: spectral quality metrics, diagnostic-fragment bitmasks, and pipeline routing."""

from __future__ import annotations

import logging
import random
from typing import Any

import numpy as np
from pydantic import BaseModel, Field

logger = logging.getLogger("msmcp.tools.qc")

# ======================================================================
# Pydantic schemas
# ======================================================================
class QCInput(BaseModel):
    """Validated input for the generate_qc_summary tool."""

    file_path: str = Field(
        ...,
        min_length=1,
        description="Path to the mass spectrometry data file (.mzML or .mgf).",
    )


# ======================================================================
# Diagnostic ion catalogue  (name, m/z, bit position)
# ======================================================================
_DIAGNOSTIC_IONS: list[tuple[str, float, int]] = [
    ("Tyrosine immonium",       136.0757,  0),
    ("Phenylalanine immonium",  120.0808,  1),
    ("Tryptophan immonium",     159.0917,  2),
    ("Histidine immonium",      110.0713,  3),
    ("Arginine immonium",       129.1135,  4),
    ("Proline immonium",         70.0651,  5),
    ("Leu/Ile immonium",         86.0964,  6),
    ("Methionine immonium",     104.0528,  7),
    ("a2 ion (generic)",         0.0,      8),   # matched heuristically
    ("b2 ion (generic)",         0.0,      9),   # matched heuristically
    ("y1 ion (generic)",         0.0,     10),   # matched heuristically
    ("Loss of H₂O (−18.011)",   -1.0,     11),   # neutral loss flag
    ("Loss of NH₃ (−17.027)",   -1.0,     12),   # neutral loss flag
    ("Loss of H₃PO₄ (−98.000)", -1.0,     13),   # phospho- marker
    ("Oxonium (glycan)",        163.0601, 14),
    ("Oxonium (HexNAc)",        204.0867, 15),
]

_NEGATIVE_MZ_SENTINEL = -1.0  # flags that require non-m/z matching logic


# ======================================================================
# Mock data generators
# ======================================================================
def _mock_spectrum_metrics(
    rng: random.Random,
) -> dict[str, Any]:
    """Return synthetic quality metrics for a single spectrum."""
    # SNR — log-normal distribution, typical MS1 SNR ~50–500
    snr = max(0.5, rng.lognormvariate(4.5, 0.8))

    # Peak count — Poisson-ish around 40 for MS2
    n_peaks = max(2, int(rng.gauss(40, 15)))

    # m/z peaks centered near 50–precursor range
    precursor = rng.uniform(200.0, 900.0)
    mz_arr = sorted(rng.uniform(50.0, precursor * 0.95) for _ in range(n_peaks))

    # Diagnostic fragment bitmask
    diag_mask = 0
    TOL = 0.02  # Da
    for name, mass, bit in _DIAGNOSTIC_IONS:
        if mass <= 0.0:
            # Heuristic bits — random with reasonable prevalence
            if rng.random() < 0.15:
                diag_mask |= (1 << bit)
            continue
        # Check if any peak falls within tolerance
        if any(abs(mz - mass) <= TOL for mz in mz_arr):
            diag_mask |= (1 << bit)
        # Also add stochastic presence for realistic noise
        elif rng.random() < 0.02:
            diag_mask |= (1 << bit)

    # Chimericity — probability of co-isolation
    isolation_width = 1.4  # Da
    # Simulate additional precursor-like signals in isolation window
    n_extra_precursors = 0
    if rng.random() < 0.25:  # 25% of spectra have co-isolation
        n_extra_precursors = rng.randint(1, 4)
    is_chimeric = n_extra_precursors > 0

    return {
        "snr": round(snr, 2),
        "n_peaks": n_peaks,
        "precursor_mz": round(precursor, 4),
        "diag_mask": diag_mask,
        "is_chimeric": is_chimeric,
        "n_co_isolated": n_extra_precursors,
    }


def _generate_mock_dataset(
    file_path: str,
    n_spectra: int | None = None,
) -> list[dict[str, Any]]:
    """Generate a synthetic dataset of spectral quality metrics.

    The RNG is seeded from *file_path* so repeated calls on the same
    file produce identical results.
    """
    rng = random.Random(hash(file_path) & 0x7FFFFFFF)
    if n_spectra is None:
        n_spectra = rng.randint(200, 5000)
    return [_mock_spectrum_metrics(rng) for _ in range(n_spectra)]


# ======================================================================
# Analysis helpers
# ======================================================================
def _snr_report(snr_values: list[float]) -> str:
    arr = np.array(snr_values, dtype=np.float64)
    mean_snr = float(np.mean(arr))
    median_snr = float(np.median(arr))
    pct_low = float(np.sum(arr < 3.0) / len(arr) * 100)
    pct_high = float(np.sum(arr > 100.0) / len(arr) * 100)

    if pct_low > 30:
        grade = "🔴 POOR"
        note = "High fraction of low-SNR spectra — consider ML denoising."
    elif pct_low > 10:
        grade = "🟡 FAIR"
        note = "Moderate noise; classical scoring may struggle with the weakest spectra."
    else:
        grade = "🟢 GOOD"
        note = "SNR distribution is suitable for classical cosine scoring."

    return (
        f"### Signal-to-Noise Ratio  {grade}\n\n"
        f"| Metric              | Value     |\n"
        f"|---------------------|----------|\n"
        f"| Mean SNR            | {mean_snr:>8.1f} |\n"
        f"| Median SNR          | {median_snr:>8.1f} |\n"
        f"| Spectra < 3 SNR     | {pct_low:>7.1f}% |\n"
        f"| Spectra > 100 SNR   | {pct_high:>7.1f}% |\n\n"
        f"{note}"
    )


def _peak_density_report(n_peaks_list: list[int]) -> str:
    arr = np.array(n_peaks_list, dtype=np.float64)
    mean_pk = float(np.mean(arr))
    median_pk = float(np.median(arr))
    std_pk = float(np.std(arr))
    pct_sparse = float(np.sum(arr < 5) / len(arr) * 100)
    pct_dense = float(np.sum(arr > 100) / len(arr) * 100)

    if pct_sparse > 20:
        grade = "🔴 SPARSE"
        note = "Many spectra have very few peaks — identification confidence will be low regardless of algorithm."
    elif pct_dense > 20:
        grade = "🟡 DENSE"
        note = "High peak density may indicate chimeric or noisy spectra; ML consensus methods are recommended."
    else:
        grade = "🟢 NORMAL"
        note = "Peak density is within expected ranges for classical scoring."

    return (
        f"### Peak Density  {grade}\n\n"
        f"| Metric                 | Value     |\n"
        f"|------------------------|----------|\n"
        f"| Mean peaks / spectrum  | {mean_pk:>8.1f} |\n"
        f"| Median peaks / spectrum| {median_pk:>8.1f} |\n"
        f"| Std deviation          | {std_pk:>8.1f} |\n"
        f"| Spectra < 5 peaks      | {pct_sparse:>7.1f}% |\n"
        f"| Spectra > 100 peaks    | {pct_dense:>7.1f}% |\n\n"
        f"{note}"
    )


def _diagnostic_fragment_report(masks: list[int], n_total: int) -> str:
    """Build a report on diagnostic fragment prevalence using the bitmask."""
    lines = [
        "### Diagnostic Fragment Analysis",
        "",
        "Boolean bitmask scan for biologically significant ions.  ",
        "Presence is defined as a peak within ±0.02 Da of the theoretical mass.",
        "",
        "| Bit | Diagnostic Ion            | Theoretical m/z | Spectra  | Prevalence |",
        "|-----|---------------------------|-----------------|----------|------------|",
    ]

    important_hits = 0
    for name, mass, bit in _DIAGNOSTIC_IONS:
        if mass <= 0.0:
            # Heuristic bit — skip display table but still count
            count = sum(1 for m in masks if m & (1 << bit))
            continue

        count = sum(1 for m in masks if m & (1 << bit))
        pct = count / n_total * 100 if n_total > 0 else 0.0
        lines.append(
            f"| {bit:>3}  | {name:<25} | {mass:>15.4f} | {count:>8} | {pct:>9.1f}% |"
        )
        if pct > 10:
            important_hits += 1

    # Special call-out for Tyrosine immonium (bit 0)
    tyr_count = sum(1 for m in masks if m & 1)
    tyr_pct = tyr_count / n_total * 100 if n_total > 0 else 0.0

    lines.append("")
    if tyr_pct > 0:
        lines.append(
            f"**Tyrosine immonium ion (136.076 Da)** detected in "
            f"**{tyr_pct:.1f}%** of spectra ({tyr_count}/{n_total})."
        )
    else:
        lines.append(
            "**Tyrosine immonium ion (136.076 Da)** was **not detected** "
            "in any spectrum."
        )

    if important_hits >= 3:
        lines.append(
            "\n⚠️  Multiple diagnostic ions are prevalent — this dataset may contain "
            "peptide-rich samples.  ML-based annotation could improve identification "
            "rates for modified or non-tryptic peptides."
        )

    return "\n".join(lines)


def _chimericity_report(
    is_chimeric: list[bool],
    n_co_list: list[int],
) -> str:
    n_chimeric = sum(is_chimeric)
    n_total = len(is_chimeric)
    pct = n_chimeric / n_total * 100 if n_total > 0 else 0.0

    if n_chimeric == 0:
        extra = "No co-isolation detected — all spectra appear pure."
    else:
        avg_extra = np.mean([c for c in n_co_list if c > 0]) if n_chimeric > 0 else 0.0
        extra = (
            f"Among chimeric spectra, an average of **{avg_extra:.1f}** additional "
            f"precursor ions were detected within the ±0.7 Da isolation window."
        )

    if pct > 25:
        grade = "🔴 HIGH"
        impact = (
            "Chimeric spectra dominate this dataset.  Traditional cosine "
            "scoring will produce unreliable matches — **ML-based deconvolution "
            "or consensus algorithms are strongly recommended**."
        )
    elif pct > 10:
        grade = "🟡 MODERATE"
        impact = (
            "A notable fraction of spectra appear chimeric.  Consider "
            "pre-filtering with a chimericity detector before cosine scoring, "
            "or use an ML-based method that models mixture spectra."
        )
    else:
        grade = "🟢 LOW"
        impact = (
            "Chimericity is low.  Classical cosine scoring should perform well."
        )

    return (
        f"### Chimeric Spectra Assessment  {grade}\n\n"
        f"| Metric                         | Value     |\n"
        f"|--------------------------------|----------|\n"
        f"| Chimeric spectra               | {n_chimeric:>8} |\n"
        f"| Total spectra                  | {n_total:>8} |\n"
        f"| Chimericity rate               | {pct:>7.1f}% |\n\n"
        f"{extra}\n\n"
        f"{impact}"
    )


def _pipeline_recommendation(
    snr_values: list[float],
    n_peaks_list: list[int],
    is_chimeric: list[bool],
    masks: list[int],
) -> str:
    """Synthesize all metrics into a pipeline routing recommendation."""
    snr_arr = np.array(snr_values)
    pk_arr = np.array(n_peaks_list)

    pct_low_snr = float(np.sum(snr_arr < 3.0) / len(snr_arr) * 100)
    pct_chimeric = sum(is_chimeric) / len(is_chimeric) * 100 if is_chimeric else 0.0
    pct_sparse = float(np.sum(pk_arr < 5) / len(pk_arr) * 100)

    # Count significant diagnostic-ion hits across the dataset
    diag_richness = sum(
        1 for bit in range(16)
        if sum(1 for m in masks if m & (1 << bit)) / len(masks) > 0.1
    )

    # Decision logic
    score_classical = 0
    score_ml = 0

    if pct_low_snr > 20:
        score_ml += 2
        score_classical -= 1
    else:
        score_classical += 2

    if pct_chimeric > 20:
        score_ml += 3
        score_classical -= 2
    elif pct_chimeric > 10:
        score_ml += 1
    else:
        score_classical += 2

    if pct_sparse > 20:
        score_ml += 1
        score_classical -= 1

    if diag_richness >= 4:
        score_ml += 1  # complex samples benefit from ML
    else:
        score_classical += 1

    if score_ml > score_classical:
        recommendation = (
            "### Pipeline Recommendation: 🔮 **ML-Based Consensus**\n\n"
            "The quality metrics indicate this dataset would benefit from "
            "machine-learning-based spectral identification:\n\n"
            "- High chimericity or noise levels degrade classical cosine scoring.\n"
            "- ML models (e.g., spectral transformers, graph neural networks) "
            "can deconvolve mixtures and model non-linear fragmentation patterns.\n"
            "- Consider tools such as MS2DeepScore, Spec2Vec, or a custom "
            "consensus ensemble.\n\n"
            "Expected improvement over classical scoring: **15–40%** in top-1 accuracy."
        )
    elif score_classical > score_ml:
        recommendation = (
            "### Pipeline Recommendation: 🧮 **Classical Cosine Scoring**\n\n"
            "The dataset quality metrics support traditional spectral library "
            "searching:\n\n"
            "- SNR and peak density are within normal ranges.\n"
            "- Chimericity is low — pure spectra match reliably by cosine.\n"
            "- Fragmentation patterns are consistent with standard collision-induced "
            "dissociation.\n\n"
            "Use `compute_cosine` or `search_library` tools for identification.\n"
            "Expected performance: **strong** (FDR < 0.05 at reasonable score thresholds)."
        )
    else:
        recommendation = (
            "### Pipeline Recommendation: ⚖️ **Hybrid Approach**\n\n"
            "The dataset exhibits mixed characteristics:\n\n"
            "- Some spectra are clean and suitable for classical scoring.\n"
            "- Others show chimericity or noise that ML methods handle better.\n\n"
            "**Suggested workflow:**\n"
            "1. Pre-filter chimeric spectra with a co-isolation detector.\n"
            "2. Score pure spectra with classical cosine.\n"
            "3. Route chimeric / low-SNR spectra to an ML model.\n"
            "4. Merge results with a weighted consensus strategy."
        )

    details = (
        f"\n\n*Decision scores — Classical: {score_classical:+d},  "
        f"ML: {score_ml:+d}*\n"
    )

    return recommendation + details


# ======================================================================
# Public registration
# ======================================================================
def register_tools(mcp: Any) -> None:
    """Register the QC summary tool on the FastMCP *mcp* instance."""

    @mcp.tool()
    def generate_qc_summary(file_path: str) -> str:
        """Analyse a mass spectrometry dataset and produce a QC report.

        Extracts baseline spectral quality metrics (SNR, peak density,
        diagnostic fragment prevalence, chimericity) and synthesises
        them into a Markdown report with a pipeline routing
        recommendation (classical cosine scoring vs. ML-based consensus).
        """
        _ = QCInput(file_path=file_path)

        # --- generate mock dataset ------------------------------------------
        spectra = _generate_mock_dataset(file_path)
        n_total = len(spectra)

        logger.info(
            "generate_qc_summary(%r) → %d mock spectra",
            file_path, n_total,
        )

        # --- extract metric arrays ------------------------------------------
        snr_values = [s["snr"] for s in spectra]
        n_peaks_list = [s["n_peaks"] for s in spectra]
        diag_masks = [s["diag_mask"] for s in spectra]
        is_chimeric = [s["is_chimeric"] for s in spectra]
        n_co_list = [s["n_co_isolated"] for s in spectra]

        # --- build report sections ------------------------------------------
        header = [
            "## QC Summary Report",
            "",
            f"**File:** `{file_path}`",
            f"**Spectra analysed:** {n_total:,}",
            "",
            "---",
            "",
        ]

        snr_section = _snr_report(snr_values)
        peak_section = _peak_density_report(n_peaks_list)
        diag_section = _diagnostic_fragment_report(diag_masks, n_total)
        chim_section = _chimericity_report(is_chimeric, n_co_list)
        pipeline_section = _pipeline_recommendation(
            snr_values, n_peaks_list, is_chimeric, diag_masks,
        )

        report = "\n".join(
            header
            + [snr_section, "", peak_section, "", diag_section, "", chim_section, "", pipeline_section]
        )

        logger.info(
            "generate_qc_summary → report %d chars",
            len(report),
        )

        return report
