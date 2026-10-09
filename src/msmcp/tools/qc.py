"""QC tools: truthful spectral quality metrics from real mzML files."""

from __future__ import annotations

import logging
from typing import Annotated, Any

import numpy as np
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from msmcp import ingest
from msmcp.errors import MalformedFileError
from msmcp.instruments import (
    DEFAULT_INSTRUMENT_CLASS,
    INSTRUMENT_CLASS_NAMES,
    InstrumentClass,
    get_profile,
)
from msmcp.security import DEFAULT_POLICY

logger = logging.getLogger("msmcp.tools.qc")


# ======================================================================
# Pydantic schemas
# ======================================================================
class QCInput(BaseModel):
    """Validated input for the generate_qc_summary tool.

    Parameter descriptions live on the tool signature, which is what the MCP
    SDK turns into the wire schema.
    """

    file_path: str = Field(..., min_length=1)
    instrument_class: InstrumentClass | None = None


# ======================================================================
# Diagnostic ion catalogue  (name, m/z, bit position)
#
# Only ions with a defined theoretical m/z are reported from real data.
# Heuristic fragments (a2/b2/y1 ions and neutral losses) require precursor
# m/z and fragmentation logic that basic mzML parsing does not provide, so
# they are intentionally omitted rather than guessed.
# ======================================================================
_DIAGNOSTIC_IONS: list[tuple[str, float, int]] = [
    ("Tyrosine immonium", 136.0757, 0),
    ("Phenylalanine immonium", 120.0808, 1),
    ("Tryptophan immonium", 159.0917, 2),
    ("Histidine immonium", 110.0713, 3),
    ("Arginine immonium", 129.1135, 4),
    ("Proline immonium", 70.0651, 5),
    ("Leu/Ile immonium", 86.0964, 6),
    ("Methionine immonium", 104.0528, 7),
    ("Oxonium (glycan)", 163.0601, 14),
    ("Oxonium (HexNAc)", 204.0867, 15),
]

# The diagnostic-ion matching window is the instrument class's
# ``diagnostic_ion_tolerance_da`` (see :mod:`msmcp.instruments`); it is threaded
# through as an argument rather than pinned here, so the value a report used is
# the value the report states.


# ======================================================================
# Metric helpers
# ======================================================================
def _estimate_snr(intensity: np.ndarray) -> float:
    """Estimate per-spectrum SNR as base peak ÷ median positive intensity.

    This is a transparent, assumption-light proxy: the base peak is treated
    as signal and the median of the positive intensities as the noise floor.
    It is reported as an *estimate*, not a calibrated instrument SNR.
    """
    positive = intensity[intensity > 0.0]
    if positive.size == 0:
        return 0.0

    signal = float(np.max(positive))
    noise = float(np.median(positive))
    if noise <= 0.0:
        return float("inf") if signal > 0.0 else 0.0
    return signal / noise


def _diagnostic_mask(mz: np.ndarray, tolerance: float) -> int:
    """Return the bitmask of diagnostic ions present within *tolerance* Da."""
    mask = 0
    for _, mass, bit in _DIAGNOSTIC_IONS:
        if np.any(np.abs(mz - mass) <= tolerance):
            mask |= 1 << bit
    return mask


# ======================================================================
# Report sections
# ======================================================================
def _snr_report(snr_values: list[float], low: float, high: float) -> str:
    arr = np.asarray(snr_values, dtype=np.float64)
    if arr.size == 0:
        return "### Signal-to-Noise Ratio\n\nNo spectra were available."

    mean_snr = float(np.mean(arr))
    median_snr = float(np.median(arr))
    min_snr = float(np.min(arr))
    max_snr = float(np.max(arr)) if np.isfinite(arr).all() else float("inf")
    pct_low = float(np.sum(arr < low) / arr.size * 100)
    pct_high = float(np.sum(arr > high) / arr.size * 100)

    return (
        "### Signal-to-Noise Ratio (estimated)\n\n"
        "| Metric              | Value     |\n"
        "|---------------------|----------|\n"
        f"| Mean SNR            | {mean_snr:>8.1f} |\n"
        f"| Median SNR          | {median_snr:>8.1f} |\n"
        f"| Min SNR             | {min_snr:>8.1f} |\n"
        f"| Max SNR             | {max_snr:>8.1f} |\n"
        f"| Spectra < {low:g} SNR     | {pct_low:>7.1f}% |\n"
        f"| Spectra > {high:g} SNR    | {pct_high:>7.1f}% |\n\n"
        "Estimator: base-peak intensity ÷ median positive intensity.  This is "
        "a transparent proxy, not a calibrated instrument SNR."
    )


def _peak_density_report(n_peaks_list: list[int]) -> str:
    arr = np.asarray(n_peaks_list, dtype=np.float64)
    if arr.size == 0:
        return "### Peak Density\n\nNo spectra were available."

    mean_pk = float(np.mean(arr))
    median_pk = float(np.median(arr))
    std_pk = float(np.std(arr))
    pct_sparse = float(np.sum(arr < 5) / arr.size * 100)
    pct_dense = float(np.sum(arr > 100) / arr.size * 100)

    return (
        "### Peak Density\n\n"
        "| Metric                 | Value     |\n"
        "|------------------------|----------|\n"
        f"| Mean peaks / spectrum  | {mean_pk:>8.1f} |\n"
        f"| Median peaks / spectrum| {median_pk:>8.1f} |\n"
        f"| Std deviation          | {std_pk:>8.1f} |\n"
        f"| Spectra < 5 peaks      | {pct_sparse:>7.1f}% |\n"
        f"| Spectra > 100 peaks    | {pct_dense:>7.1f}% |\n"
    )


def _diagnostic_fragment_report(
    masks: list[int], n_total: int, tolerance: float
) -> str:
    lines = [
        "### Diagnostic Fragment Analysis",
        "",
        f"Presence is defined as a peak within ±{tolerance:g} Da of the "
        "theoretical m/z.",
        "",
        "| Bit | Diagnostic Ion            | Theoretical m/z | Spectra  | Prevalence |",
        "|-----|---------------------------|-----------------|----------|------------|",
    ]

    for name, mass, bit in _DIAGNOSTIC_IONS:
        count = sum(1 for m in masks if m & (1 << bit))
        pct = count / n_total * 100 if n_total > 0 else 0.0
        lines.append(
            f"| {bit:>3}  | {name:<25} | {mass:>15.4f} | {count:>8} | {pct:>9.1f}% |"
        )

    return "\n".join(lines)


# ======================================================================
# Public registration
# ======================================================================
def register_tools(mcp: Any) -> None:
    """Register the QC summary tool on the MCPServer *mcp* instance."""

    @mcp.tool(
        title="Generate a QC summary",
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False),
    )
    def generate_qc_summary(
        file_path: Annotated[
            str,
            Field(
                min_length=1,
                description="Path to a local MS file (.mzML, .mzML.gz, .mgf "
                "or .imzML), absolute or relative to the server working "
                "directory.",
            ),
        ],
        instrument_class: Annotated[
            InstrumentClass | None,
            Field(
                description=(
                    "Instrument class whose documented defaults set the "
                    "diagnostic-ion matching window and the SNR reporting "
                    f"bands (default '{DEFAULT_INSTRUMENT_CLASS}': ±0.02 Da, "
                    "5/20 SNR).  One of: " + ", ".join(INSTRUMENT_CLASS_NAMES) + "."
                ),
            ),
        ] = None,
    ) -> str:
        """Report quality-control metrics for a local MS acquisition.

        Reads .mzML, .mzML.gz, .mgf and .imzML (imaging) files; the reader is
        chosen from the file extension.  Scans every spectrum and reports
        spectrum and total-ion-current counts, per-spectrum peak density, an
        *estimated* signal-to-noise ratio (base peak / median positive
        intensity, a transparent proxy rather than a calibrated instrument
        SNR), and the prevalence of ten diagnostic immonium and oxonium
        fragment ions.

        The two experimental thresholds — the diagnostic-ion matching window
        and the SNR bands used for the low/high prevalence rows — come from the
        named `instrument_class` (default 'generic': +/-0.02 Da and 5/20 SNR,
        MSMCP's v1.0 values), and the values actually applied are stated in the
        report, so a QC verdict is never separated from the assumption behind
        it.

        Use it to judge whether an acquisition is worth searching.  Unlike
        `load_mzml_summary`, this reads the whole file rather than the first
        few spectra, so it is slower on large runs.
        """
        _ = QCInput(file_path=file_path, instrument_class=instrument_class)
        profile = get_profile(instrument_class)
        diagnostic_tolerance = profile.diagnostic_ion_tolerance_da

        source = ingest.resolve_source(file_path, DEFAULT_POLICY)

        n_total = 0
        total_tic = 0.0
        snr_values: list[float] = []
        n_peaks_list: list[int] = []
        diag_masks: list[int] = []

        for spectrum in source.iter_spectra():
            n_total += 1
            tic = spectrum.tic
            total_tic += tic
            snr_values.append(_estimate_snr(spectrum.intensity))
            n_peaks_list.append(spectrum.n_peaks)
            diag_masks.append(_diagnostic_mask(spectrum.mz, diagnostic_tolerance))

        if n_total == 0:
            raise MalformedFileError(f"'{file_path}' contains no spectra to analyse.")

        mean_tic = total_tic / n_total
        header = [
            "## QC Summary Report",
            "",
            f"**File:** `{file_path}`",
            f"**Format:** {source.format} (reader: `{source.backend}`)",
            f"**Spectra analysed:** {n_total:,}",
            f"**Total ion current:** {total_tic:.6e}",
            f"**Mean TIC / spectrum:** {mean_tic:.6e}",
            f"**Instrument class:** {profile.instrument_class} "
            f"(diagnostic-ion window ±{diagnostic_tolerance:g} Da, "
            f"SNR bands {profile.snr_low:g}/{profile.snr_high:g})",
            "",
            "---",
            "",
        ]

        report = "\n".join(
            [
                *header,
                _snr_report(snr_values, profile.snr_low, profile.snr_high),
                "",
                _peak_density_report(n_peaks_list),
                "",
                _diagnostic_fragment_report(diag_masks, n_total, diagnostic_tolerance),
            ]
        )

        logger.info(
            "generate_qc_summary(%r, class=%s) → %d spectra, TIC=%.3e, report %d chars",
            file_path,
            profile.instrument_class,
            n_total,
            total_tic,
            len(report),
        )

        return report
