"""Bounded, scan-level chromatogram calculations for MS acquisitions."""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from typing import Literal

import numpy as np

from msmcp.mzml import Spectrum
from msmcp.provenance import Provenance, SourceRef, provenance_for
from msmcp.security import SpectrumCountExceededError

ToleranceUnit = Literal["ppm", "Da"]
MAX_CHROMATOGRAM_SPECTRA = 10_000
MAX_XIC_TARGETS = 20


def tolerance_bounds(
    target_mz: float, tolerance: float, tolerance_unit: ToleranceUnit
) -> tuple[float, float]:
    """Return the inclusive lower and upper m/z limits for an XIC window."""
    if not math.isfinite(target_mz) or target_mz <= 0:
        raise ValueError("target_mz must be finite and greater than zero")
    if not math.isfinite(tolerance) or tolerance < 0:
        raise ValueError("tolerance must be finite and non-negative")
    if tolerance_unit == "ppm":
        half_width = target_mz * tolerance * 1e-6
    elif tolerance_unit == "Da":
        half_width = tolerance
    else:
        raise ValueError("tolerance_unit must be 'ppm' or 'Da'")
    lower, upper = target_mz - half_width, target_mz + half_width
    if not all(math.isfinite(bound) for bound in (half_width, lower, upper)):
        raise ValueError("tolerance produces a non-finite m/z window")
    return lower, upper


@dataclass(frozen=True, slots=True)
class XICTrace:
    target_mz: float
    lower_mz: float
    upper_mz: float
    intensities: list[float | None]


@dataclass(frozen=True, slots=True)
class ChromatogramTraces:
    scan_indices: list[int]
    scan_ordinals: list[int]
    source_spectrum_indices: list[int | None]
    retention_times_min: list[float | None]
    tic_intensities: list[float | None]
    bpc_intensities: list[float | None]
    xic: list[XICTrace]


@dataclass(frozen=True, slots=True)
class MissingObservation:
    scan_ordinal: int
    source_spectrum_index: int | None
    ms_level: int | None
    retention_time_min: float | None
    reason: str


@dataclass(frozen=True, slots=True)
class ChromatogramResult:
    schema_version: str
    acquisition: dict[str, object]
    chromatograms: ChromatogramTraces
    extraction_parameters: dict[str, object]
    completeness: dict[str, object]
    missing_observations: list[MissingObservation]
    warnings: list[str]
    provenance: Provenance

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-safe structured result."""
        return {
            "schema_version": self.schema_version,
            "acquisition": self.acquisition,
            "chromatograms": asdict(self.chromatograms),
            "extraction_parameters": self.extraction_parameters,
            "completeness": self.completeness,
            "missing_observations": [
                asdict(item) for item in self.missing_observations
            ],
            "warnings": list(self.warnings),
            "provenance": self.provenance.to_dict(),
        }


def extract_chromatograms(
    spectra: Iterable[Spectrum],
    *,
    targets_mz: list[float],
    tolerance: float,
    tolerance_unit: ToleranceUnit,
    ms_level: int = 1,
    retention_time_range: tuple[float, float] | None = None,
    max_spectra: int = MAX_CHROMATOGRAM_SPECTRA,
    source: SourceRef | None = None,
) -> ChromatogramResult:
    """Compute TIC, BPC and multiple XICs in one bounded streaming pass.

    ``max_spectra`` counts every source spectrum scanned, including spectra at
    unselected MS levels and outside the optional retention-time range.  If an
    additional spectrum exists after the limit, the operation raises instead
    of returning a partial acquisition.
    """
    if not 1 <= ms_level <= 10:
        raise ValueError("ms_level must be between 1 and 10")
    if not 1 <= max_spectra <= MAX_CHROMATOGRAM_SPECTRA:
        raise ValueError(
            f"max_spectra must be between 1 and {MAX_CHROMATOGRAM_SPECTRA}"
        )
    if len(targets_mz) > MAX_XIC_TARGETS:
        raise ValueError(f"at most {MAX_XIC_TARGETS} XIC targets may be requested")
    bounds = [
        tolerance_bounds(target, tolerance, tolerance_unit) for target in targets_mz
    ]
    if retention_time_range is not None:
        rt_start, rt_end = retention_time_range
        if (
            not math.isfinite(rt_start)
            or not math.isfinite(rt_end)
            or rt_start > rt_end
            or rt_start < 0
        ):
            raise ValueError(
                "retention_time_range must be finite, non-negative, and ordered"
            )

    scanned = 0
    selected_level_count = 0
    level_counts: Counter[str] = Counter()
    representation_counts: Counter[str] = Counter()
    selected_representation_counts: Counter[str] = Counter()
    polarity_counts: Counter[str] = Counter()
    selected_polarity_counts: Counter[str] = Counter()
    all_retention_times: list[float] = []
    missing_retention_time_spectra = 0
    empty_peak_spectra = 0
    selected_empty_peak_spectra = 0
    scan_indices: list[int] = []
    scan_ordinals: list[int] = []
    source_indices: list[int | None] = []
    retention_times: list[float | None] = []
    tic_values: list[float | None] = []
    bpc_values: list[float | None] = []
    xic_values: list[list[float | None]] = [[] for _ in targets_mz]
    missing: list[MissingObservation] = []

    for ordinal, spectrum in enumerate(spectra):
        if scanned >= max_spectra:
            raise SpectrumCountExceededError(
                f"Chromatogram extraction exceeds max_spectra={max_spectra}; "
                "the limit counts scanned spectra, including unselected MS "
                "levels and spectra outside the retention-time range. No "
                "partial result was returned."
            )
        scanned += 1
        level_key = (
            str(spectrum.ms_level) if spectrum.ms_level is not None else "unknown"
        )
        level_counts[level_key] += 1
        representation = spectrum.representation
        representation_key = (
            representation if representation in {"centroid", "profile"} else "unknown"
        )
        representation_counts[representation_key] += 1
        polarity_key = (
            spectrum.polarity
            if spectrum.polarity in {"positive", "negative"}
            else "unknown"
        )
        polarity_counts[polarity_key] += 1
        if spectrum.mz.size == 0 and spectrum.intensity.size == 0:
            empty_peak_spectra += 1
        rt = spectrum.retention_time
        rt_value = (
            float(rt) if rt is not None and math.isfinite(rt) and rt >= 0 else None
        )
        valid_rt = rt_value is not None
        if rt_value is not None:
            all_retention_times.append(rt_value)
        else:
            missing_retention_time_spectra += 1
        if spectrum.ms_level != ms_level:
            continue
        selected_level_count += 1
        selected_representation_counts[representation_key] += 1
        selected_polarity_counts[polarity_key] += 1
        if (
            retention_time_range is not None
            and valid_rt
            and rt_value is not None
            and not (retention_time_range[0] <= rt_value <= retention_time_range[1])
        ):
            continue
        if retention_time_range is not None and not valid_rt:
            missing.append(
                MissingObservation(
                    ordinal,
                    spectrum.index,
                    spectrum.ms_level,
                    None,
                    "retention_time_unavailable_for_filter",
                )
            )
            continue

        source_index = spectrum.index
        scan_indices.append(source_index if source_index is not None else ordinal)
        scan_ordinals.append(ordinal)
        source_indices.append(source_index)
        retention_times.append(rt_value)

        mz = np.asarray(spectrum.mz, dtype=np.float64)
        intensity = np.asarray(spectrum.intensity, dtype=np.float64)
        invalid_arrays = (
            mz.ndim != 1
            or intensity.ndim != 1
            or mz.size != intensity.size
            or not np.all(np.isfinite(mz))
            or not np.all(np.isfinite(intensity))
        )
        if not invalid_arrays and mz.size == 0:
            selected_empty_peak_spectra += 1
        reason: str | None = "retention_time_unavailable" if not valid_rt else None
        if invalid_arrays:
            reason = "invalid_peak_arrays"
        if reason is not None:
            tic_values.append(None)
            bpc_values.append(None)
            for values in xic_values:
                values.append(None)
            missing.append(
                MissingObservation(
                    ordinal,
                    spectrum.index,
                    spectrum.ms_level,
                    rt_value,
                    reason,
                )
            )
            continue

        with np.errstate(over="ignore", invalid="ignore"):
            tic = float(np.sum(intensity, dtype=np.float64))
            bpc = float(np.max(intensity)) if intensity.size else None
            xic_scan_values = [
                float(
                    np.sum(intensity[(mz >= lower) & (mz <= upper)], dtype=np.float64)
                )
                for lower, upper in bounds
            ]
        aggregates = [tic, *xic_scan_values]
        if bpc is not None:
            aggregates.append(bpc)
        if not all(math.isfinite(value) for value in aggregates):
            tic_values.append(None)
            bpc_values.append(None)
            for values in xic_values:
                values.append(None)
            missing.append(
                MissingObservation(
                    ordinal,
                    spectrum.index,
                    spectrum.ms_level,
                    rt_value,
                    "non_finite_aggregate",
                )
            )
            continue

        tic_values.append(tic)
        bpc_values.append(bpc)
        for values, xic_value in zip(xic_values, xic_scan_values, strict=True):
            values.append(xic_value)

    rt_coverage: dict[str, float | None] = {
        "start_min": min(all_retention_times) if all_retention_times else None,
        "end_min": max(all_retention_times) if all_retention_times else None,
    }
    traces = ChromatogramTraces(
        scan_indices=scan_indices,
        scan_ordinals=scan_ordinals,
        source_spectrum_indices=source_indices,
        retention_times_min=retention_times,
        tic_intensities=tic_values,
        bpc_intensities=bpc_values,
        xic=[
            XICTrace(target, lower, upper, values)
            for target, (lower, upper), values in zip(
                targets_mz, bounds, xic_values, strict=True
            )
        ],
    )
    parameters: dict[str, object] = {
        "targets_mz": list(targets_mz),
        "tolerance": tolerance,
        "tolerance_unit": tolerance_unit,
        "xic_aggregation": "sum_inclusive_mz_window",
        "tic_calculation": "sum_all_reported_intensities_per_scan",
        "bpc_calculation": "maximum_reported_intensity_per_scan",
        "ms_level": ms_level,
        "retention_time_range_min": list(retention_time_range)
        if retention_time_range is not None
        else None,
        "max_spectra": max_spectra,
        "resource_limit_counts": "scanned_spectra",
    }
    warnings = [
        "Intensity units are input-defined; absolute intensities may not be comparable across acquisitions or instrument settings.",
        "No smoothing, normalization, baseline correction, alignment, blank subtraction, or identification was applied.",
    ]
    if representation_counts["unknown"]:
        warnings.append(
            f"Centroid/profile representation is unknown for {representation_counts['unknown']} scanned spectrum/spectra."
        )
    if targets_mz and selected_representation_counts["unknown"]:
        warnings.append(
            f"Centroid/profile representation is unknown for {selected_representation_counts['unknown']} selected MS-level spectrum/spectra; XIC values sum reported points in the window and are not asserted to be centroid sums."
        )
    if targets_mz and selected_representation_counts["profile"]:
        warnings.append(
            f"{selected_representation_counts['profile']} selected profile spectrum/spectra were present; XIC values sum sampled profile intensities in the m/z window, not centroid peaks."
        )
    if empty_peak_spectra:
        warnings.append(
            f"{empty_peak_spectra} scanned spectrum/spectra had valid empty peak arrays; TIC and XIC values are zero, while BPC is null because an empty scan has no base peak."
        )
    if level_counts["unknown"]:
        warnings.append(
            f"MS level is unavailable for {level_counts['unknown']} scanned spectrum/spectra; those scans were not assigned to the selected level."
        )
    if missing_retention_time_spectra:
        warnings.append(
            f"Retention time is missing or invalid for {missing_retention_time_spectra} scanned spectrum/spectra; no time value was inferred."
        )
    if polarity_counts["unknown"]:
        warnings.append(
            f"Polarity is unavailable for {polarity_counts['unknown']} scanned spectrum/spectra."
        )
    if missing:
        warnings.append(
            f"{len(missing)} selected scan observation(s) were missing or invalid and are represented as null or listed in missing_observations."
        )
    if scanned == 0:
        warnings.append("The source contained no spectra.")

    provenance = provenance_for(
        "extract_chromatograms",
        parameters=parameters,
        sources=[source] if source is not None else [],
    )
    return ChromatogramResult(
        schema_version="1.0",
        acquisition={
            "scanned_spectra": scanned,
            "spectrum_counts_by_ms_level": dict(sorted(level_counts.items())),
            "selected_ms_level": ms_level,
            "selected_ms_level_spectra": selected_level_count,
            "retention_time_coverage_min": rt_coverage,
            "retention_time_units": "minutes",
            "representation_counts": {
                key: representation_counts[key]
                for key in ("centroid", "profile", "unknown")
            },
            "selected_representation_counts": {
                key: selected_representation_counts[key]
                for key in ("centroid", "profile", "unknown")
            },
            "polarity_counts": {
                key: polarity_counts[key] for key in ("positive", "negative", "unknown")
            },
            "selected_polarity_counts": {
                key: selected_polarity_counts[key]
                for key in ("positive", "negative", "unknown")
            },
            "empty_peak_array_spectra": empty_peak_spectra,
            "selected_empty_peak_array_spectra": selected_empty_peak_spectra,
            "missing_retention_time_spectra": missing_retention_time_spectra,
        },
        chromatograms=traces,
        extraction_parameters=parameters,
        completeness={
            "full_acquisition_processed": True,
            "scanned_spectra": scanned,
            "max_spectra": max_spectra,
            "resource_limit_counts": "scanned_spectra",
            "returned_points": len(scan_indices),
        },
        missing_observations=missing,
        warnings=warnings,
        provenance=provenance,
    )
