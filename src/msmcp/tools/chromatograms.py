"""Read-only, structured LC-MS/MS chromatogram extraction tool."""

from __future__ import annotations

from dataclasses import asdict
from typing import Annotated, Any, Literal

from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from msmcp import ingest
from msmcp.chromatograms import (
    MAX_CHROMATOGRAM_SPECTRA,
    MAX_XIC_TARGETS,
    ChromatogramResult,
)
from msmcp.chromatograms import (
    extract_chromatograms as calculate_chromatograms,
)
from msmcp.errors import MalformedFileError, MsmcpError, UnsupportedFormatError
from msmcp.provenance import SourceRef
from msmcp.security import DEFAULT_POLICY


class ChromatogramInput(BaseModel):
    """Validated arguments for ``extract_chromatograms``."""

    file_path: str = Field(..., min_length=1)
    targets_mz: list[Annotated[float, Field(gt=0.0, allow_inf_nan=False)]] = Field(
        default_factory=list, max_length=MAX_XIC_TARGETS
    )
    tolerance: float = Field(default=5.0, ge=0.0, allow_inf_nan=False)
    tolerance_unit: Literal["ppm", "Da"] = "ppm"
    ms_level: int = Field(default=1, ge=1, le=10, strict=True)
    retention_time_range_min: (
        list[Annotated[float, Field(allow_inf_nan=False)]] | None
    ) = Field(default=None, min_length=2, max_length=2)
    max_spectra: int = Field(
        strict=True,
        default=MAX_CHROMATOGRAM_SPECTRA,
        ge=1,
        le=MAX_CHROMATOGRAM_SPECTRA,
    )


class ChromatogramParameters(BaseModel):
    targets_mz: list[float]
    tolerance: float
    tolerance_unit: Literal["ppm", "Da"]
    xic_aggregation: str
    tic_calculation: str
    bpc_calculation: str
    ms_level: int
    retention_time_range_min: list[float] | None
    max_spectra: int
    resource_limit_counts: str


class ChromatogramSource(BaseModel):
    file_path: str
    format: str
    reader_backend: str
    size_bytes: int | None
    digest: str | None


class ChromatogramXIC(BaseModel):
    target_mz: float
    lower_mz_inclusive: float
    upper_mz_inclusive: float
    aggregation: str
    intensity_units: str
    intensities: list[float | None]


class ChromatogramData(BaseModel):
    native_scan_ids: list[str | None]
    scan_indices: list[int]
    scan_ordinals: list[int]
    source_spectrum_indices: list[int | None]
    retention_times_min: list[float | None]
    tic_intensities: list[float | None]
    bpc_intensities: list[float | None]
    xic: list[ChromatogramXIC]
    mz_units: str = "m/z"
    retention_time_units: str = "minutes"
    intensity_units: str = "input_defined"


class ChromatogramAcquisition(BaseModel):
    scanned_spectra: int
    spectrum_counts_by_ms_level: dict[str, int]
    selected_ms_level: int
    selected_ms_level_spectra: int
    retention_time_coverage_min: dict[str, float | None]
    retention_time_units: str
    representation_counts: dict[str, int]
    selected_representation_counts: dict[str, int]
    polarity_counts: dict[str, int]
    selected_polarity_counts: dict[str, int]
    empty_peak_array_spectra: int
    selected_empty_peak_array_spectra: int
    missing_retention_time_spectra: int


class MissingScanObservation(BaseModel):
    scan_ordinal: int
    source_spectrum_index: int | None
    ms_level: int | None
    retention_time_min: float | None
    reason: str


class ChromatogramCompleteness(BaseModel):
    full_acquisition_processed: bool
    scanned_spectra: int
    max_spectra: int
    resource_limit_counts: str
    returned_points: int


class ChromatogramToolResult(BaseModel):
    schema_version: Literal["1.0"]
    source: ChromatogramSource
    acquisition: ChromatogramAcquisition
    chromatograms: ChromatogramData
    extraction_parameters: ChromatogramParameters
    completeness: ChromatogramCompleteness
    missing_observations: list[MissingScanObservation]
    warnings: list[str]
    provenance: dict[str, Any]


def register_tools(mcp: Any) -> None:
    @mcp.tool(
        title="Extract scan-level chromatograms",
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False),
    )
    def extract_chromatograms(
        file_path: Annotated[
            str,
            Field(
                min_length=1,
                description="Path to a local .mzML or .mzML.gz acquisition inside the configured allowed root.",
            ),
        ],
        targets_mz: Annotated[
            list[float] | None,
            Field(
                max_length=MAX_XIC_TARGETS,
                description=f"Target m/z values for simultaneous XICs; up to {MAX_XIC_TARGETS}. Use an empty list or null for TIC and BPC only.",
            ),
        ] = None,
        tolerance: Annotated[
            float,
            Field(
                ge=0.0,
                description="Half-width of each XIC window, in the selected tolerance_unit.",
            ),
        ] = 5.0,
        tolerance_unit: Annotated[
            Literal["ppm", "Da"],
            Field(
                description="Tolerance units: ppm relative to target m/z, or absolute Da."
            ),
        ] = "ppm",
        ms_level: Annotated[
            int,
            Field(
                strict=True,
                ge=1,
                le=10,
                description="MS level included in the chromatogram traces; defaults to MS1.",
            ),
        ] = 1,
        retention_time_range_min: Annotated[
            list[float] | None,
            Field(
                min_length=2,
                max_length=2,
                description="Optional inclusive [start, end] retention-time range in minutes.",
            ),
        ] = None,
        max_spectra: Annotated[
            int,
            Field(
                ge=1,
                le=MAX_CHROMATOGRAM_SPECTRA,
                strict=True,
                description=f"Maximum source spectra to scan, across all MS levels and retention times; hard maximum {MAX_CHROMATOGRAM_SPECTRA}.",
            ),
        ] = MAX_CHROMATOGRAM_SPECTRA,
    ) -> ChromatogramToolResult:
        """Return structured TIC, BPC and optional XIC traces from one bounded pass.

        TIC is the sum of reported intensities per selected scan. BPC is its
        maximum reported intensity, or null for an empty scan. Each XIC sums
        all reported points in the inclusive m/z window. Representation is
        reported when the acquisition states centroid or profile; unknown
        representation remains unknown and is called out in warnings. No
        smoothing, normalization, alignment, baseline correction, blank
        subtraction, or identification is performed. The tool scans the full
        source to report acquisition counts and coverage; the optional RT range
        filters returned trace points only.
        """
        validated = ChromatogramInput(
            file_path=file_path,
            targets_mz=targets_mz or [],
            tolerance=tolerance,
            tolerance_unit=tolerance_unit,
            ms_level=ms_level,
            retention_time_range_min=retention_time_range_min,
            max_spectra=max_spectra,
        )
        source = ingest.resolve_source(validated.file_path, DEFAULT_POLICY)
        if source.format != "mzML":
            raise UnsupportedFormatError(
                "extract_chromatograms supports mzML and mzML.gz files only."
            )
        before = source.path.stat()
        source_ref = SourceRef.from_path(
            source.path,
            format=source.format,
            backend=source.backend,
            digest_limit_bytes=DEFAULT_POLICY.max_file_size_bytes,
        )
        if source_ref.digest is None:
            raise MsmcpError(
                "Source authentication failed within the configured file-size budget; no chromatogram returned."
            )
        declared_count = source.declared_spectrum_count()
        if declared_count < 0:
            raise MalformedFileError("Negative declared spectrum count.")
        retention_time_range = validated.retention_time_range_min
        rt_range = (
            (float(retention_time_range[0]), float(retention_time_range[1]))
            if retention_time_range is not None
            else None
        )
        result: ChromatogramResult = calculate_chromatograms(
            source.iter_spectra(),
            targets_mz=validated.targets_mz,
            tolerance=validated.tolerance,
            tolerance_unit=validated.tolerance_unit,
            ms_level=validated.ms_level,
            retention_time_range=rt_range,
            max_spectra=validated.max_spectra,
            source=source_ref,
        )
        if result.acquisition["scanned_spectra"] != declared_count:
            raise MalformedFileError(
                "Declared spectrum count disagrees with the scanned acquisition; no partial result returned."
            )
        # Recheck both the policy boundary and identity after reading. A local
        # source must stay immutable throughout authentication and extraction.
        ingest.resolve_source(validated.file_path, DEFAULT_POLICY)
        after = source.path.stat()
        if any(
            getattr(before, key) != getattr(after, key)
            for key in ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
        ):
            raise MsmcpError(
                "Source changed during extraction; no chromatogram returned."
            )
        return ChromatogramToolResult(
            schema_version="1.0",
            source=ChromatogramSource(
                file_path=source_ref.path,
                format=source_ref.format or "unknown",
                reader_backend=source_ref.backend or "unknown",
                size_bytes=source_ref.size_bytes,
                digest=source_ref.digest,
            ),
            acquisition=ChromatogramAcquisition.model_validate(result.acquisition),
            chromatograms=ChromatogramData(
                native_scan_ids=result.chromatograms.native_scan_ids,
                scan_indices=result.chromatograms.scan_indices,
                scan_ordinals=result.chromatograms.scan_ordinals,
                source_spectrum_indices=result.chromatograms.source_spectrum_indices,
                retention_times_min=result.chromatograms.retention_times_min,
                tic_intensities=result.chromatograms.tic_intensities,
                bpc_intensities=result.chromatograms.bpc_intensities,
                xic=[
                    ChromatogramXIC(
                        target_mz=xic.target_mz,
                        lower_mz_inclusive=xic.lower_mz,
                        upper_mz_inclusive=xic.upper_mz,
                        aggregation="sum_inclusive_mz_window",
                        intensity_units="input_defined",
                        intensities=xic.intensities,
                    )
                    for xic in result.chromatograms.xic
                ],
            ),
            extraction_parameters=ChromatogramParameters.model_validate(
                result.extraction_parameters
            ),
            completeness=ChromatogramCompleteness.model_validate(result.completeness),
            missing_observations=[
                MissingScanObservation.model_validate(asdict(item))
                for item in result.missing_observations
            ],
            warnings=result.warnings,
            provenance=result.provenance.to_dict(),
        )
