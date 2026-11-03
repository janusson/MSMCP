"""QC tools: truthful spectral quality metrics from real mzML files."""

from __future__ import annotations

import itertools
import logging
from dataclasses import dataclass
from typing import Annotated, Any, Literal

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
from msmcp.mzml import Spectrum
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
_MAX_INTENSITY_ROWS = 20


@dataclass(frozen=True, slots=True)
class _PeakIntensitySummary:
    """Descriptive distribution statistics for one spectrum's recorded intensities."""

    spectrum_index: int | None
    ms_level: int | None
    representation: str | None
    n_values: int
    n_positive_finite: int
    n_zero: int
    n_negative_finite: int
    n_nonfinite: int
    median_positive: float | None
    mad_positive: float | None
    applicability: Literal["descriptive_only", "not_applicable_no_positive_values"]


def _finite_median(values: np.ndarray) -> float | None:
    """Compute a median without overflowing when averaging two finite values."""
    count = int(values.size)
    if count == 0:
        return None
    middle = count // 2
    if count % 2:
        partitioned = np.partition(values, middle)
        return float(partitioned[middle])
    partitioned = np.partition(values, (middle - 1, middle))
    lower = float(partitioned[middle - 1])
    upper = float(partitioned[middle])
    return lower + (upper - lower) * 0.5


def _summarise_peak_intensity(spectrum: Spectrum) -> _PeakIntensitySummary:
    """Summarise observed values, not background noise or SNR.

    Only finite positive values enter the median and unscaled MAD. Zero,
    negative, and non-finite entries are counted separately, so an all-zero or
    empty spectrum is not converted into a reassuring numerical zero.
    """
    intensity = np.asarray(spectrum.intensity, dtype=np.float64)
    finite = np.isfinite(intensity)
    positive = intensity[finite & (intensity > 0.0)]
    n_nonfinite = int(np.count_nonzero(~finite))
    n_zero = int(np.count_nonzero(finite & (intensity == 0.0)))
    n_negative = int(np.count_nonzero(finite & (intensity < 0.0)))

    if positive.size == 0:
        return _PeakIntensitySummary(
            spectrum_index=spectrum.index,
            ms_level=spectrum.ms_level,
            representation=spectrum.representation,
            n_values=intensity.size,
            n_positive_finite=0,
            n_zero=n_zero,
            n_negative_finite=n_negative,
            n_nonfinite=n_nonfinite,
            median_positive=None,
            mad_positive=None,
            applicability="not_applicable_no_positive_values",
        )

    median = _finite_median(positive)
    assert median is not None
    deviations = np.abs(positive - median)
    mad = _finite_median(deviations)
    assert mad is not None
    return _PeakIntensitySummary(
        spectrum_index=spectrum.index,
        ms_level=spectrum.ms_level,
        representation=spectrum.representation,
        n_values=intensity.size,
        n_positive_finite=int(positive.size),
        n_zero=n_zero,
        n_negative_finite=n_negative,
        n_nonfinite=n_nonfinite,
        median_positive=median,
        mad_positive=mad,
        applicability="descriptive_only",
    )


def _intensity_distribution_report(
    summaries: list[_PeakIntensitySummary],
) -> str:
    """Report descriptive intensity statistics without calling them a noise floor."""
    applicable = sum(
        summary.applicability == "descriptive_only" for summary in summaries
    )
    lines = [
        "### Observed Peak-Intensity Distribution",
        "",
        f"Finite positive intensity values were present in {applicable}/{len(summaries)} "
        "analysed spectra.",
        "",
        "| Spectrum index | MS level | Representation | Positive / total | Zero | Negative | "
        "Non-finite | Median positive | Unscaled MAD | Applicability |",
        "|---:|---:|---|---:|---:|---:|---:|---:|---:|---|",
    ]
    displayed = summaries[:_MAX_INTENSITY_ROWS]
    for summary in displayed:
        index = (
            "not stated"
            if summary.spectrum_index is None
            else str(summary.spectrum_index)
        )
        level = "not stated" if summary.ms_level is None else f"MS{summary.ms_level}"
        representation = summary.representation or "not stated"
        median = (
            "N/A"
            if summary.median_positive is None
            else f"{summary.median_positive:.4g}"
        )
        mad = "N/A" if summary.mad_positive is None else f"{summary.mad_positive:.4g}"
        applicability = (
            "descriptive only; not noise"
            if summary.applicability == "descriptive_only"
            else "not applicable: no finite positive values"
        )
        lines.append(
            f"| {index} | {level} | {representation} | "
            f"{summary.n_positive_finite}/{summary.n_values} | {summary.n_zero} | "
            f"{summary.n_negative_finite} | {summary.n_nonfinite} | "
            f"{median} | {mad} | {applicability} |"
        )
    if len(summaries) > len(displayed):
        lines.append(
            f"{len(summaries) - len(displayed)} additional spectra were summarised "
            f"but omitted from the {len(displayed)}-row display cap."
        )
    lines.extend(
        [
            "",
            "For each spectrum, the median and raw (unscaled) median absolute deviation "
            "are computed over finite intensities > 0 only. Zero, negative, and "
            "non-finite values are excluded from those statistics and counted above. "
            "Intensity scale and units are source-dependent; MSMCP does not rescale "
            "them or infer whether the source pre-normalised them.",
            "These are descriptive statistics of recorded array values, not a noise "
            "floor, calibrated SNR, threshold, or cross-acquisition quality score. "
            "No k·MAD cutoff is applied. Sparse, zero-censored, or highly skewed peak "
            "lists can have MAD=0 or otherwise misleading distributions.",
            f"Metric provenance: finite-positive filter (>0); median; unscaled MAD; "
            f"{_MAX_INTENSITY_ROWS}-row display cap; calculations cover all "
            f"{len(summaries)} analysed spectra.",
        ]
    )
    return "\n".join(lines)


def _isolation_window_report(
    windows: list[tuple[int | None, float]],
    *,
    n_total: int,
    n_unusable: int,
) -> str:
    """Expose the legacy scalar isolation width without implying purity."""
    lines = [
        "### Isolation Window Metadata (not purity)",
        "",
        f"A finite positive scalar width was available for {len(windows)}/{n_total} "
        "analysed spectra.",
    ]
    displayed = windows[:_MAX_INTENSITY_ROWS]
    for index, width in displayed:
        label = "not stated" if index is None else str(index)
        lines.append(f"- Spectrum {label}: width {width:g} m/z.")
    if len(windows) > len(displayed):
        lines.append(
            f"{len(windows) - len(displayed)} additional widths omitted from the "
            f"{len(displayed)}-row display cap."
        )
    if n_unusable:
        lines.append(f"Unusable non-finite/non-positive width values: {n_unusable}.")
    lines.append(
        "This is the existing scalar from the first complete isolation-window entry "
        "(lower + upper offset); it is not the isolation target or the raw offset "
        "pair. Width alone gives no co-isolated ion abundances, so purity is "
        "not estimated."
    )
    return "\n".join(lines)


def _diagnostic_mask(
    mz: np.ndarray,
    intensity: np.ndarray,
    tolerance: float,
) -> int:
    """Return the bitmask of diagnostic ions present within *tolerance* Da with positive intensity."""
    if mz.size == 0 or intensity.size == 0:
        return 0
    valid = (intensity > 0.0) & np.isfinite(intensity) & np.isfinite(mz) & (mz > 0.0)
    if not np.any(valid):
        return 0
    pos_mz = mz[valid]
    mask = 0
    for _, mass, bit in _DIAGNOSTIC_IONS:
        # Preserve the inclusive boundary when float subtraction rounds one ULP high.
        lower = np.nextafter(mass - tolerance, -np.inf)
        upper = np.nextafter(mass + tolerance, np.inf)
        if np.any((pos_mz >= lower) & (pos_mz <= upper)):
            mask |= 1 << bit
    return mask


# ======================================================================
# Report sections
# ======================================================================


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
    masks: list[int],
    n_evaluated: int,
    tolerance: float,
    *,
    context_note: str | None = None,
    not_applicable_message: str | None = None,
) -> str:
    lines = [
        "### Observed Diagnostic m/z Matches",
        "",
        f"Presence is a finite positive m/z with finite positive intensity within ±{tolerance:g} Da of the "
        "theoretical m/z.",
        "A positive match is an observed feature only. It is not noise-supported "
        "fragment evidence and is not a compound identification; background and interference can also match.",
        "Match frequency is a fraction of eligible spectra with a catalogue m/z "
        "coincidence, not an identification probability.",
    ]
    if not_applicable_message is not None:
        lines.extend(["", not_applicable_message])
        return "\n".join(lines)

    if context_note:
        lines.append(context_note)
    lines.extend(
        [
            "",
            "| Bit | Catalogue label (not assignment) | Reference m/z | Spectra matched | Match frequency |",
            "|---:|---|---:|---:|---:|",
        ]
    )

    for name, mass, bit in _DIAGNOSTIC_IONS:
        count = sum(1 for m in masks if m & (1 << bit))
        pct = count / n_evaluated * 100 if n_evaluated > 0 else 0.0
        lines.append(f"| {bit} | {name} | {mass:.4f} | {count} | {pct:.1f}% |")

    return "\n".join(lines)


def _interpretation_limits_report() -> str:
    """Separate observed features from unsupported chemical or noise claims."""
    return "\n".join(
        [
            "### Metric Provenance and Inference Limits",
            "",
            "The statements below apply to the metrics for the processed subset and "
            "source/reader named above.",
            "",
            "- The intensity table describes recorded values; its provenance rule "
            "and parameters are stated with the table. It does not estimate background "
            "noise or calibrated SNR.",
            "- Diagnostic entries are positive finite m/z coincidences in the stated "
            "MS-level context. They are not noise-supported detections or an "
            "identification.",
            "- Isolation purity is not estimated. An isolation-window width alone "
            "does not report the abundance of co-isolated ions; MS level alone does "
            "not establish a particular activation pathway.",
            "- No identification confidence, probability, or false-discovery rate "
            "is calculated by this QC report.",
        ]
    )


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
                    "Instrument class whose documented default sets the "
                    f"diagnostic-ion m/z window (default '{DEFAULT_INSTRUMENT_CLASS}': "
                    "±0.02 Da). It does not set a noise floor. One of: "
                    + ", ".join(INSTRUMENT_CLASS_NAMES)
                    + "."
                ),
            ),
        ] = None,
    ) -> str:
        """Report quality-control metrics for a local MS acquisition.

        Reads .mzML, .mzML.gz, .mgf and .imzML (imaging) files; the reader is
        chosen from the file extension. Scans at most the configured per-request
        spectrum limit and reports when full-acquisition coverage is unconfirmed.
        Metrics include total-ion-current counts, peak density, a per-spectrum
        summary of the observed intensity distribution (median and unscaled MAD
        over finite positive values), and diagnostic m/z coincidences.

        The intensity statistics are descriptive only. The available arrays do
        not identify background-only observations, so these statistics are not
        interpreted as a noise floor or SNR and no k·MAD cutoff is applied.
        The `instrument_class` sets only the diagnostic-ion matching window;
        this is stated in the report. Positive-intensity mass matches are not
        noise-supported fragment evidence or chemical identification.

        Use it to judge whether an acquisition is worth searching. Unlike
        `load_mzml_summary`, it may scan up to the configured spectrum limit;
        it never presents a capped summary as covering the full acquisition.
        """
        _ = QCInput(file_path=file_path, instrument_class=instrument_class)
        profile = get_profile(instrument_class)
        diagnostic_tolerance = profile.diagnostic_ion_tolerance_da

        source = ingest.resolve_source(file_path, DEFAULT_POLICY)

        spectrum_limit = DEFAULT_POLICY.max_spectra
        n_total = 0
        total_tic = 0.0
        intensity_summaries: list[_PeakIntensitySummary] = []
        isolation_windows: list[tuple[int | None, float]] = []
        n_unusable_isolation_windows = 0
        n_peaks_list: list[int] = []
        n_ms1 = 0
        n_ms2_plus = 0
        n_unknown_level = 0
        n_invalid_level = 0
        diag_masks_ms2: list[int] = []
        diag_masks_unknown: list[int] = []
        spectra = source.iter_spectra()
        try:
            for spectrum in itertools.islice(spectra, spectrum_limit):
                n_total += 1
                tic = spectrum.tic
                total_tic += tic
                intensity_summaries.append(_summarise_peak_intensity(spectrum))
                width = spectrum.isolation_window
                if width is not None:
                    if np.isfinite(width) and width > 0.0:
                        isolation_windows.append((spectrum.index, float(width)))
                    else:
                        n_unusable_isolation_windows += 1
                n_peaks_list.append(spectrum.n_peaks)
                level = spectrum.ms_level
                if level == 1:
                    n_ms1 += 1
                elif level is not None and level >= 2:
                    n_ms2_plus += 1
                    diag_masks_ms2.append(
                        _diagnostic_mask(
                            spectrum.mz, spectrum.intensity, diagnostic_tolerance
                        )
                    )
                elif level is None:
                    n_unknown_level += 1
                    diag_masks_unknown.append(
                        _diagnostic_mask(
                            spectrum.mz, spectrum.intensity, diagnostic_tolerance
                        )
                    )
                else:
                    n_invalid_level += 1
        finally:
            close = getattr(spectra, "close", None)
            if callable(close):
                close()

        if n_total == 0:
            raise MalformedFileError(f"'{file_path}' contains no spectra to analyse.")

        if source.format == "imzML" and not any(n_peaks_list):
            raise MalformedFileError(
                f"No peaks were readable in the {n_total:,} imzML pixels analysed "
                f"within the {spectrum_limit:,}-spectrum limit. Cannot confirm "
                "the paired .ibd payload is readable within this budget; later "
                "pixels may contain signal. QC metrics are not reported."
            )

        mean_tic = total_tic / n_total
        coverage = (
            f"**Coverage:** the {spectrum_limit:,}-spectrum processing limit was "
            "reached; full-acquisition coverage is not confirmed. Metrics below "
            f"describe only the {n_total:,} spectra analysed."
            if n_total >= spectrum_limit
            else "**Coverage:** complete; the reader reached the end of the acquisition."
        )
        header = [
            "## QC Summary Report",
            "",
            f"**File:** `{file_path}`",
            f"**Format:** {source.format} (reader: `{source.backend}`)",
            f"**Spectra analysed:** {n_total:,}",
            coverage,
            f"**Total ion current (analysed spectra):** {total_tic:.6e}",
            f"**Mean TIC / analysed spectrum:** {mean_tic:.6e}",
            f"**Instrument class:** {profile.instrument_class} "
            f"(diagnostic-ion window ±{diagnostic_tolerance:g} Da)",
            "",
            "---",
            "",
        ]

        ms1_str = f"{n_ms1} MS1 spectrum" if n_ms1 == 1 else f"{n_ms1} MS1 spectra"
        ms2_str = (
            f"{n_ms2_plus} MS/MS spectrum"
            if n_ms2_plus == 1
            else f"{n_ms2_plus} MS/MS spectra"
        )
        unk_str = (
            f"{n_unknown_level} spectrum"
            if n_unknown_level == 1
            else f"{n_unknown_level} spectra"
        )
        invalid_level_str = (
            f"{n_invalid_level} invalid declared MS level value"
            if n_invalid_level == 1
            else f"{n_invalid_level} invalid declared MS level values"
        )

        not_applicable_message: str | None = None
        context_note: str | None = None
        diag_report_masks: list[int] = []
        n_diag_evaluated: int = 0

        if n_ms2_plus > 0:
            diag_report_masks = diag_masks_ms2
            n_diag_evaluated = n_ms2_plus
            excluded = []
            if n_ms1:
                excluded.append(f"{ms1_str} excluded")
            if n_unknown_level:
                excluded.append(
                    f"{unk_str} with absent or unparseable MS level omitted"
                )
            if n_invalid_level:
                excluded.append(f"{invalid_level_str} (<1) omitted")
            context_note = f"Evaluated across {ms2_str}"
            if excluded:
                context_note += " (" + "; ".join(excluded) + ")"
            context_note += "."
        elif n_unknown_level > 0:
            diag_report_masks = diag_masks_unknown
            n_diag_evaluated = n_unknown_level
            excluded = []
            if n_ms1:
                excluded.append(f"{ms1_str} excluded")
            if n_invalid_level:
                excluded.append(f"{invalid_level_str} (<1) excluded")
            context_note = (
                f"MS level was absent or unparseable in the input; evaluated across "
                f"{unk_str}"
            )
            if excluded:
                context_note += " (" + "; ".join(excluded) + ")"
            context_note += "."
        elif n_invalid_level:
            excluded = []
            if n_ms1:
                excluded.append(f"{ms1_str} declared MS1")
            excluded.append(f"{invalid_level_str} (<1)")
            not_applicable_message = (
                "No eligible diagnostic spectra: "
                + "; ".join(excluded)
                + ". No declared MS/MS or absent/unparseable-level spectra are available."
            )
        else:
            not_applicable_message = (
                "Diagnostic reference-mass matching is evaluated only for tandem "
                f"MS (MS/MS, MS2+) spectra. All {n_total:,} analysed "
                "spectra are declared MS1; 0 MS/MS spectra available."
            )

        diag_report = _diagnostic_fragment_report(
            diag_report_masks,
            n_diag_evaluated,
            diagnostic_tolerance,
            context_note=context_note,
            not_applicable_message=not_applicable_message,
        )

        report = "\n".join(
            [
                *header,
                _intensity_distribution_report(intensity_summaries),
                "",
                _peak_density_report(n_peaks_list),
                "",
                diag_report,
                "",
                _isolation_window_report(
                    isolation_windows,
                    n_total=n_total,
                    n_unusable=n_unusable_isolation_windows,
                ),
                "",
                _interpretation_limits_report(),
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
