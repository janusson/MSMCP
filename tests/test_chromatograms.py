"""Scientific behavior tests for scan-level chromatogram extraction."""

from __future__ import annotations

from typing import cast

import numpy as np
import pytest

from msmcp.chromatograms import extract_chromatograms, tolerance_bounds
from msmcp.mzml import Spectrum, iter_spectra
from msmcp.security import SecurityPolicy, SpectrumCountExceededError
from msmcp.tools import chromatograms as chromatogram_module
from msmcp.tools.chromatograms import ChromatogramToolResult


def _spectrum(
    index: int,
    ms_level: int | None,
    retention_time: float | None,
    mz: list[float],
    intensity: list[float],
    representation: str | None = "centroid",
    polarity: str | None = "positive",
) -> Spectrum:
    return Spectrum(
        index=index,
        ms_level=ms_level,
        retention_time=retention_time,
        precursor_mz=None,
        mz=np.asarray(mz, dtype=np.float64),
        intensity=np.asarray(intensity, dtype=np.float64),
        polarity=polarity,
        representation=representation,
    )


def test_tolerance_bounds_use_ppm_and_da() -> None:
    assert tolerance_bounds(200.0, 5.0, "ppm") == pytest.approx((199.999, 200.001))
    assert tolerance_bounds(200.0, 0.01, "Da") == pytest.approx((199.99, 200.01))


def test_tolerance_bounds_reject_non_finite_calculated_windows() -> None:
    with pytest.raises(ValueError, match="non-finite m/z window"):
        tolerance_bounds(1e308, 1e308, "Da")


def test_mzml_reader_reports_centroid_profile_and_unknown(
    mzml_with_representation_metadata,
) -> None:
    spectra = list(iter_spectra(mzml_with_representation_metadata))
    assert [spectrum.representation for spectrum in spectra] == [
        "centroid",
        "profile",
        None,
    ]


def test_xic_includes_both_exact_mz_boundaries() -> None:
    result = extract_chromatograms(
        [_spectrum(7, 1, 1.0, [199.999, 200.001, 200.0011], [2.0, 3.0, 50.0])],
        targets_mz=[200.0],
        tolerance=0.001,
        tolerance_unit="Da",
        max_spectra=5,
    )
    assert result.chromatograms.xic[0].intensities == [5.0]


def test_multiple_targets_use_the_same_single_scan_pass() -> None:
    result = extract_chromatograms(
        [_spectrum(0, 1, 0.5, [100.0, 223.0634, 297.0822], [5.0, 11.0, 13.0])],
        targets_mz=[223.0634, 297.0822],
        tolerance=5.0,
        tolerance_unit="ppm",
        max_spectra=5,
    )
    assert [trace.intensities for trace in result.chromatograms.xic] == [
        [11.0],
        [13.0],
    ]
    assert result.chromatograms.tic_intensities == [29.0]
    assert result.chromatograms.bpc_intensities == [13.0]


def test_ms_level_filter_keeps_source_scan_indices_and_rt_order() -> None:
    result = extract_chromatograms(
        [
            _spectrum(10, 1, 2.0, [100.0], [4.0]),
            _spectrum(11, 2, 2.1, [100.0], [900.0]),
            _spectrum(12, 1, 2.0, [100.0], [6.0]),
        ],
        targets_mz=[100.0],
        tolerance=0.0,
        tolerance_unit="Da",
        max_spectra=5,
    )
    assert result.chromatograms.scan_indices == [10, 12]
    assert result.chromatograms.retention_times_min == [2.0, 2.0]
    assert result.chromatograms.xic[0].intensities == [4.0, 6.0]


def test_empty_valid_scan_is_zero_but_invalid_arrays_are_missing() -> None:
    result = extract_chromatograms(
        [
            _spectrum(1, 1, 0.1, [], []),
            _spectrum(2, 1, 0.2, [100.0, 200.0], [3.0]),
        ],
        targets_mz=[100.0],
        tolerance=0.1,
        tolerance_unit="Da",
        max_spectra=5,
    )
    assert result.chromatograms.xic[0].intensities == [0.0, None]
    assert result.chromatograms.tic_intensities == [0.0, None]
    assert result.chromatograms.bpc_intensities == [None, None]
    assert result.missing_observations[0].source_spectrum_index == 2


def test_overflowed_aggregates_are_reported_as_missing() -> None:
    result = extract_chromatograms(
        [_spectrum(8, 1, 1.0, [100.0, 100.0], [1e308, 1e308])],
        targets_mz=[100.0],
        tolerance=0.0,
        tolerance_unit="Da",
        max_spectra=1,
    )
    assert result.chromatograms.tic_intensities == [None]
    assert result.chromatograms.bpc_intensities == [None]
    assert result.chromatograms.xic[0].intensities == [None]
    assert result.missing_observations[0].reason == "non_finite_aggregate"


def test_missing_rt_is_not_replaced_or_emitted_as_zero() -> None:
    result = extract_chromatograms(
        [_spectrum(3, 1, None, [100.0], [12.0])],
        targets_mz=[100.0],
        tolerance=0.1,
        tolerance_unit="Da",
        max_spectra=5,
    )
    assert result.chromatograms.retention_times_min == [None]
    assert result.chromatograms.xic[0].intensities == [None]
    assert result.missing_observations[0].reason == "retention_time_unavailable"


def test_rt_range_is_inclusive_and_does_not_reorder_scans() -> None:
    result = extract_chromatograms(
        [
            _spectrum(4, 1, 1.5, [100.0], [1.0]),
            _spectrum(5, 1, 1.0, [100.0], [2.0]),
            _spectrum(6, 1, 2.0, [100.0], [3.0]),
        ],
        targets_mz=[100.0],
        tolerance=0.1,
        tolerance_unit="Da",
        retention_time_range=(1.0, 1.5),
        max_spectra=5,
    )
    assert result.chromatograms.scan_indices == [4, 5]
    assert result.chromatograms.retention_times_min == [1.5, 1.0]


def test_representation_unknown_and_profile_are_reported() -> None:
    result = extract_chromatograms(
        [
            _spectrum(1, 1, 1.0, [100.0], [2.0], representation=None),
            _spectrum(2, 1, 1.1, [100.0], [3.0], representation="profile"),
        ],
        targets_mz=[100.0],
        tolerance=0.1,
        tolerance_unit="Da",
        max_spectra=5,
    )
    assert result.acquisition["representation_counts"] == {
        "centroid": 0,
        "profile": 1,
        "unknown": 1,
    }
    assert any("profile" in warning.lower() for warning in result.warnings)
    assert any("unknown" in warning.lower() for warning in result.warnings)


def test_polarity_metadata_is_counted_without_assuming_missing_values() -> None:
    result = extract_chromatograms(
        [
            _spectrum(1, 1, 1.0, [100.0], [2.0], polarity="positive"),
            _spectrum(2, 2, 1.1, [100.0], [3.0], polarity="negative"),
            _spectrum(3, 1, 1.2, [100.0], [4.0], polarity=None),
        ],
        targets_mz=[100.0],
        tolerance=0.1,
        tolerance_unit="Da",
        max_spectra=5,
    )
    assert result.acquisition["polarity_counts"] == {
        "positive": 1,
        "negative": 1,
        "unknown": 1,
    }
    assert result.acquisition["selected_polarity_counts"] == {
        "positive": 1,
        "negative": 0,
        "unknown": 1,
    }


def test_resource_limit_counts_scanned_spectra_not_selected_points() -> None:
    spectra = iter(
        [
            _spectrum(0, 2, 1.0, [100.0], [1.0]),
            _spectrum(1, 2, 1.1, [100.0], [1.0]),
            _spectrum(2, 1, 1.2, [100.0], [1.0]),
        ]
    )
    with pytest.raises(SpectrumCountExceededError, match="scanned spectra"):
        extract_chromatograms(
            spectra,
            targets_mz=[100.0],
            tolerance=0.1,
            tolerance_unit="Da",
            ms_level=1,
            max_spectra=2,
        )


def test_results_are_deterministic_and_include_calculation_provenance() -> None:
    spectra = [_spectrum(0, 1, 1.0, [100.0], [2.0])]
    first = extract_chromatograms(
        spectra, targets_mz=[100.0], tolerance=0.1, tolerance_unit="Da", max_spectra=5
    ).to_dict()
    second = extract_chromatograms(
        spectra, targets_mz=[100.0], tolerance=0.1, tolerance_unit="Da", max_spectra=5
    ).to_dict()
    assert first["chromatograms"] == second["chromatograms"]
    provenance = cast(dict[str, object], first["provenance"])
    parameters = cast(dict[str, object], first["extraction_parameters"])
    assert provenance["operation"] == "extract_chromatograms"
    assert parameters["xic_aggregation"] == "sum_inclusive_mz_window"


def test_mcp_tool_returns_structured_data_with_file_provenance(
    chromatogram_tools, valid_mzml, monkeypatch
) -> None:
    monkeypatch.setattr(
        chromatogram_module,
        "DEFAULT_POLICY",
        SecurityPolicy(allowed_root=valid_mzml.parent),
    )
    result = chromatogram_tools["extract_chromatograms"](
        file_path=str(valid_mzml),
        targets_mz=[200.0],
        tolerance=0.0,
        tolerance_unit="Da",
        ms_level=1,
        max_spectra=5,
    )

    assert isinstance(result, ChromatogramToolResult)
    payload = result.model_dump(mode="json")
    assert payload["acquisition"]["scanned_spectra"] == 3
    assert payload["acquisition"]["spectrum_counts_by_ms_level"] == {"1": 3}
    assert payload["chromatograms"]["tic_intensities"] == [60.0, 10.0, 10.0]
    assert payload["chromatograms"]["xic"][0]["intensities"] == [20.0, 0.0, 0.0]
    assert payload["chromatograms"]["retention_times_min"] == pytest.approx(
        [0.0, 1 / 60, 2 / 60]
    )
    assert payload["source"]["digest"].startswith("sha256:")
    assert payload["provenance"]["sources"][0]["backend"] == "msmcp.mzml"
    assert payload["provenance"]["software"]["versions"]["msmcp"]
    assert payload["completeness"]["full_acquisition_processed"] is True


def test_zero_intensity_centroid_remains_a_valid_zero_xic() -> None:
    result = extract_chromatograms(
        [_spectrum(0, 1, 1.0, [100.0], [0.0])],
        targets_mz=[100.0],
        tolerance=0.0,
        tolerance_unit="Da",
        max_spectra=1,
    )
    assert result.chromatograms.xic[0].intensities == [0.0]
    assert result.missing_observations == []


def test_tool_refuses_to_return_a_partial_acquisition(
    chromatogram_tools, valid_mzml, monkeypatch
) -> None:
    monkeypatch.setattr(
        chromatogram_module,
        "DEFAULT_POLICY",
        SecurityPolicy(allowed_root=valid_mzml.parent),
    )
    with pytest.raises(SpectrumCountExceededError, match="No partial result"):
        chromatogram_tools["extract_chromatograms"](
            file_path=str(valid_mzml),
            targets_mz=[],
            max_spectra=2,
        )
