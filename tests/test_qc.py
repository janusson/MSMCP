"""Tests for the truthful QC summary tool."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest
from conftest import _mzml_xml  # type: ignore[import-not-found]

from msmcp.errors import InaccessiblePathError, MalformedFileError
from msmcp.mzml import Spectrum
from msmcp.security import SecurityPolicy
from msmcp.tools import qc


def _spectrum_with_intensities(
    intensities: list[float],
    *,
    index: int | None = 0,
    ms_level: int | None = None,
    representation: str | None = None,
) -> Spectrum:
    intensity = np.asarray(intensities, dtype=np.float64)
    mz = np.arange(intensity.size, dtype=np.float64) + 50.0
    return Spectrum(
        index=index,
        ms_level=ms_level,
        retention_time=None,
        precursor_mz=None,
        mz=mz,
        intensity=intensity,
        representation=representation,
    )


class TestQC:
    def test_empty_imaging_prefix_stays_unverified_without_reading_later_pixels(
        self,
        qc_tools: dict[str, Callable[..., str]],
        valid_mzml: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        policy = SecurityPolicy(allowed_root=valid_mzml.parent, max_spectra=1)
        source = qc.ingest.resolve_source(valid_mzml, policy)
        first = next(source.iter_spectra())
        empty = replace(first, mz=np.array([]), intensity=np.array([]))
        closed: list[bool] = []

        class SparseImagingSource:
            format = "imzML"
            backend = "massflow.imzML"

            def iter_spectra(self):
                try:
                    yield empty
                    raise AssertionError(
                        "QC scanned past its budget for payload checks"
                    )
                finally:
                    closed.append(True)

        monkeypatch.setattr(qc, "DEFAULT_POLICY", policy)
        monkeypatch.setattr(
            qc.ingest, "resolve_source", lambda _path, _policy: SparseImagingSource()
        )
        with pytest.raises(MalformedFileError, match="later pixels may contain signal"):
            qc_tools["generate_qc_summary"](str(valid_mzml))
        assert closed == [True]

    @pytest.mark.parametrize("limit", [1, 2])
    def test_capped_imzml_rejects_unreadable_binary_payload(
        self,
        qc_tools: dict[str, Callable[..., str]],
        imzml_fixture: Path,
        monkeypatch: pytest.MonkeyPatch,
        limit: int,
    ) -> None:
        imzml_fixture.with_suffix(".ibd").write_bytes(b"\x00\x01\x02")
        monkeypatch.setattr(
            qc,
            "DEFAULT_POLICY",
            SecurityPolicy(allowed_root=imzml_fixture.parent, max_spectra=limit),
        )
        with pytest.raises(MalformedFileError, match="payload"):
            qc_tools["generate_qc_summary"](str(imzml_fixture))

    def test_capped_nonempty_imzml_keeps_partial_coverage(
        self,
        qc_tools: dict[str, Callable[..., str]],
        imzml_fixture: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            qc,
            "DEFAULT_POLICY",
            SecurityPolicy(allowed_root=imzml_fixture.parent, max_spectra=1),
        )
        out = qc_tools["generate_qc_summary"](str(imzml_fixture))
        assert "**Spectra analysed:** 1" in out
        assert "full-acquisition coverage is not confirmed" in out

    def test_qc_summary(
        self,
        qc_tools: dict[str, Callable[..., str]],
        valid_mzml: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            qc, "DEFAULT_POLICY", SecurityPolicy(allowed_root=valid_mzml.parent)
        )
        out = qc_tools["generate_qc_summary"](str(valid_mzml))

        assert "**Spectra analysed:** 3" in out
        assert "Total ion current (analysed spectra):" in out
        assert "Mean TIC / analysed spectrum:" in out
        assert "Coverage:** complete" in out
        assert "Observed Peak-Intensity Distribution" in out
        assert "Peak Density" in out
        assert "Evidence and Interpretation Limits" in out
        assert "not a noise floor, calibrated SNR" in out
        assert "Isolation purity is not estimated" in out
        assert "Signal-to-Noise Ratio" not in out

    def test_median_and_unscaled_mad_match_controlled_signal_values(self) -> None:
        spectrum = _spectrum_with_intensities(
            [75.0, 80.0, 90.0, 100.0], ms_level=2, representation="centroid"
        )
        summary = qc._summarise_peak_intensity(spectrum)

        assert summary.n_values == 4
        assert summary.n_positive_finite == 4
        assert summary.median_positive == pytest.approx(85.0)
        assert summary.mad_positive == pytest.approx(7.5)
        assert summary.applicability == "descriptive_only"
        assert summary.representation == "centroid"

    def test_zero_dominated_spectrum_reports_censoring_and_not_a_zero_noise_floor(
        self,
    ) -> None:
        spectrum = _spectrum_with_intensities([0.0, 0.0, 0.0, 0.02])
        summary = qc._summarise_peak_intensity(spectrum)

        assert summary.n_values == 4
        assert summary.n_positive_finite == 1
        assert summary.n_zero == 3
        assert summary.median_positive == pytest.approx(0.02)
        assert summary.mad_positive == 0.0
        assert summary.applicability == "descriptive_only"
        report = qc._intensity_distribution_report([summary])
        assert "MAD=0" in report or "| 0.02 | 0 |" in report
        assert "not a noise floor" in report
        assert "No k·MAD cutoff is applied" in report

    @pytest.mark.parametrize(
        ("intensities", "n_zero", "n_negative", "n_nonfinite"),
        [
            ([], 0, 0, 0),
            ([0.0, 0.0], 2, 0, 0),
            ([float("nan"), float("inf"), float("-inf"), -1.0], 0, 1, 3),
        ],
    )
    def test_no_positive_finite_values_are_not_applicable(
        self,
        intensities: list[float],
        n_zero: int,
        n_negative: int,
        n_nonfinite: int,
    ) -> None:
        summary = qc._summarise_peak_intensity(
            _spectrum_with_intensities(intensities, index=None)
        )

        assert summary.n_positive_finite == 0
        assert summary.n_zero == n_zero
        assert summary.n_negative_finite == n_negative
        assert summary.n_nonfinite == n_nonfinite
        assert summary.median_positive is None
        assert summary.mad_positive is None
        assert summary.applicability == "not_applicable_no_positive_values"
        report = qc._intensity_distribution_report([summary])
        assert "not applicable: no finite positive values" in report
        assert "| not stated | not stated | not stated |" in report
        assert "N/A" in report

    def test_skewed_and_repeated_values_do_not_become_noise_estimates(self) -> None:
        summary = qc._summarise_peak_intensity(
            _spectrum_with_intensities([2.0, 2.0, 2.0, 1000.0])
        )

        assert summary.median_positive == pytest.approx(2.0)
        assert summary.mad_positive == 0.0
        assert summary.applicability == "descriptive_only"

    def test_intensity_statistics_cannot_label_low_peaks_as_baseline_or_fragments(
        self,
    ) -> None:
        observed = [0.1, 0.2, 0.3, 100.0]
        # The same centroided values could be interpreted as three background
        # features plus one analyte peak, or as three real low-abundance fragments.
        background_interpretation = qc._summarise_peak_intensity(
            _spectrum_with_intensities(observed, ms_level=2, representation="centroid")
        )
        fragment_interpretation = qc._summarise_peak_intensity(
            _spectrum_with_intensities(observed, ms_level=2, representation="centroid")
        )

        assert background_interpretation.median_positive == pytest.approx(0.25)
        assert background_interpretation.mad_positive == pytest.approx(0.1)
        assert fragment_interpretation == background_interpretation
        assert background_interpretation.applicability == "descriptive_only"

    def test_median_calculation_does_not_overflow_on_large_finite_values(self) -> None:
        largest = float(np.finfo(np.float64).max)
        summary = qc._summarise_peak_intensity(
            _spectrum_with_intensities([largest, largest])
        )

        assert summary.median_positive == largest
        assert summary.mad_positive == 0.0

    def test_intensity_summary_does_not_mutate_source_arrays(self) -> None:
        spectrum = _spectrum_with_intensities([0.0, 1.0, 2.0, float("nan")])
        spectrum.mz.setflags(write=False)
        spectrum.intensity.setflags(write=False)
        mz_before = spectrum.mz.copy()
        intensity_before = spectrum.intensity.copy()

        qc._summarise_peak_intensity(spectrum)

        np.testing.assert_array_equal(spectrum.mz, mz_before)
        np.testing.assert_array_equal(spectrum.intensity, intensity_before)
        assert not spectrum.mz.flags.writeable
        assert not spectrum.intensity.flags.writeable

    def test_a_weak_positive_diagnostic_match_is_reported_as_an_observation_only(
        self,
    ) -> None:
        mask = qc._diagnostic_mask(
            np.array([136.0757]), np.array([1.0e-300]), tolerance=0.02
        )

        assert mask & 1
        report = qc._diagnostic_fragment_report([mask], 1, 0.02)
        assert "observed feature" in report
        assert "not noise-supported" in report

    def test_distribution_display_is_bounded_and_discloses_omitted_rows(self) -> None:
        summaries = [
            qc._summarise_peak_intensity(
                _spectrum_with_intensities([float(index + 1)], index=index)
            )
            for index in range(qc._MAX_INTENSITY_ROWS + 3)
        ]

        report = qc._intensity_distribution_report(summaries)

        assert (
            "additional spectra were summarised but omitted from the 20-row display cap"
            in report
        )
        assert report.count("| descriptive only; not noise |") == qc._MAX_INTENSITY_ROWS

    def test_qc_stops_at_policy_limit_and_discloses_incomplete_coverage(
        self,
        qc_tools: dict[str, Callable[..., str]],
        valid_mzml: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        policy = SecurityPolicy(allowed_root=valid_mzml.parent, max_spectra=2)
        real_source = qc.ingest.resolve_source(valid_mzml, policy)
        first_two = list(real_source.iter_spectra())[:2]

        class SourceAtLimit:
            format = real_source.format
            backend = real_source.backend

            def iter_spectra(self):
                yield from first_two
                raise AssertionError("QC read beyond its configured spectrum limit")

        monkeypatch.setattr(qc, "DEFAULT_POLICY", policy)
        monkeypatch.setattr(
            qc.ingest, "resolve_source", lambda _path, _policy: SourceAtLimit()
        )
        out = qc_tools["generate_qc_summary"](str(valid_mzml))

        assert "**Spectra analysed:** 2" in out
        assert "processing limit was reached" in out
        assert "full-acquisition coverage is not confirmed" in out
        assert "Total ion current (analysed spectra):" in out

    def test_qc_truncated_file(
        self,
        qc_tools: dict[str, Callable[..., str]],
        truncated_mzml: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            qc, "DEFAULT_POLICY", SecurityPolicy(allowed_root=truncated_mzml.parent)
        )
        with pytest.raises(MalformedFileError):
            qc_tools["generate_qc_summary"](str(truncated_mzml))

    def test_qc_missing_file(
        self,
        qc_tools: dict[str, Callable[..., str]],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(qc, "DEFAULT_POLICY", SecurityPolicy(allowed_root=tmp_path))
        with pytest.raises(InaccessiblePathError):
            qc_tools["generate_qc_summary"](str(tmp_path / "missing.mzML"))

    def test_diagnostic_mask_requires_finite_positive_intensity(self) -> None:
        """Zero, negative and non-finite intensities are not observed matches."""
        mz = np.array([136.0757, 120.0808, 159.0917, 110.0713, 129.1135])
        intensity = np.array([0.0, -10.0, np.nan, np.inf, -np.inf])
        mask = qc._diagnostic_mask(mz, intensity, tolerance=0.02)
        assert mask == 0
        assert qc._diagnostic_mask(np.array([]), np.array([]), tolerance=0.02) == 0

    @pytest.mark.parametrize("direction", [-1.0, 1.0])
    @pytest.mark.parametrize(("name", "mass", "bit"), qc._DIAGNOSTIC_IONS)
    def test_diagnostic_mask_includes_exact_tolerance_edges(
        self,
        name: str,
        mass: float,
        bit: int,
        direction: float,
    ) -> None:
        tolerance = 0.02
        mz = np.array([mass + direction * tolerance])

        mask = qc._diagnostic_mask(mz, np.array([1.0]), tolerance)

        assert mask & (1 << bit), (
            f"{name} at its exact {direction:+g} Da edge was missed"
        )
        outward = np.inf if direction > 0.0 else -np.inf
        two_ulps_outside = np.nextafter(np.nextafter(mz[0], outward), outward)
        outside_mask = qc._diagnostic_mask(
            np.array([two_ulps_outside]), np.array([1.0]), tolerance
        )
        assert not outside_mask & (1 << bit), (
            f"{name} two ULPs outside its {direction:+g} Da edge matched"
        )

    def test_diagnostic_match_report_disclaims_noise_support_and_identity(self) -> None:
        mask = qc._diagnostic_mask(
            np.array([136.0757]), np.array([1.0]), tolerance=0.02
        )

        report = qc._diagnostic_fragment_report([mask], 1, 0.02)

        assert "not noise-supported" in report
        assert "not a compound identification" in report

    def test_qc_diagnostic_fragment_excludes_ms1_and_calculates_ms2_prevalence(
        self,
        qc_tools: dict[str, Callable[..., str]],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """MS1 peaks are not MS2 fragments; prevalence denominator is MS2 spectra."""
        path = tmp_path / "mixed_levels.mzML"
        spectra = [
            # MS1 scan with peak at Tyrosine immonium m/z (136.0757) - must be excluded as fragment
            {
                "mz": [136.0757, 500.0],
                "intensity": [1.0e5, 2.0e5],
                "ms_level": 1,
            },
            # MS2 scan 1 with Histidine immonium m/z (110.0713)
            {
                "mz": [110.0713, 300.0],
                "intensity": [5.0e4, 1.0e5],
                "ms_level": 2,
                "precursor_mz": 500.0,
            },
            # MS2 scan 2 with no diagnostic ions
            {
                "mz": [200.0, 300.0],
                "intensity": [1.0e4, 2.0e4],
                "ms_level": 2,
                "precursor_mz": 600.0,
            },
        ]
        path.write_text(_mzml_xml(spectra), encoding="utf-8")
        monkeypatch.setattr(qc, "DEFAULT_POLICY", SecurityPolicy(allowed_root=tmp_path))
        out = qc_tools["generate_qc_summary"](str(path))

        # Tyrosine immonium (present in MS1 only) must NOT be counted as a fragment
        assert (
            "|   0  | Tyrosine immonium         |        136.0757 |        0 |"
            "       0.0% |" in out
        )
        # Histidine immonium (present in 1 of 2 MS2 scans) must have 50.0% prevalence, not 33.3%
        assert (
            "|   3  | Histidine immonium        |        110.0713 |        1 |"
            "      50.0% |" in out
        )
        # Report must disclose that MS1 spectra were excluded and how many MS/MS spectra were evaluated
        assert "2 MS/MS spectra" in out
        assert "1 MS1 spectrum excluded" in out

    def test_qc_ms3_spectra_are_eligible_tandem_spectra(
        self,
        qc_tools: dict[str, Callable[..., str]],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        path = tmp_path / "ms3.mzML"
        path.write_text(
            _mzml_xml(
                [
                    {"mz": [70.0651], "intensity": [5.0], "ms_level": 3},
                    {"mz": [110.0713], "intensity": [5.0], "ms_level": 2},
                ]
            ),
            encoding="utf-8",
        )
        monkeypatch.setattr(qc, "DEFAULT_POLICY", SecurityPolicy(allowed_root=tmp_path))

        out = qc_tools["generate_qc_summary"](str(path))

        assert "2 MS/MS spectra" in out
        assert (
            f"| {5:>3}  | {'Proline immonium':<25} | {70.0651:>15.4f} | "
            f"{1:>8} | {50.0:>9.1f}% |" in out
        )
        assert (
            f"| {3:>3}  | {'Histidine immonium':<25} | {110.0713:>15.4f} | "
            f"{1:>8} | {50.0:>9.1f}% |" in out
        )

    def test_qc_unknown_level_is_excluded_from_denominator_when_ms2_exists(
        self,
        qc_tools: dict[str, Callable[..., str]],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        path = tmp_path / "mixed-known-unknown.mgf"
        path.write_text(
            "BEGIN IONS\nMSLEVEL=2\n110.0713 10\nEND IONS\n"
            "BEGIN IONS\n136.0757 10\nEND IONS\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(qc, "DEFAULT_POLICY", SecurityPolicy(allowed_root=tmp_path))

        out = qc_tools["generate_qc_summary"](str(path))

        assert "Evaluated across 1 MS/MS spectrum" in out
        assert "1 spectrum with absent or unparseable MS level omitted" in out
        assert (
            "|   3  | Histidine immonium        |        110.0713 |        1 |" in out
        )
        assert (
            "|   0  | Tyrosine immonium         |        136.0757 |        0 |" in out
        )

    def test_qc_diagnostic_fragment_ms1_only_file(
        self,
        qc_tools: dict[str, Callable[..., str]],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An MS1-only file states that diagnostic fragment analysis is not applicable."""
        path = tmp_path / "ms1_only.mzML"
        spectra = [
            {"mz": [136.0757, 500.0], "intensity": [1.0e5, 2.0e5], "ms_level": 1},
            {"mz": [200.0, 300.0], "intensity": [1.0e4, 2.0e4], "ms_level": 1},
        ]
        path.write_text(_mzml_xml(spectra), encoding="utf-8")
        monkeypatch.setattr(qc, "DEFAULT_POLICY", SecurityPolicy(allowed_root=tmp_path))
        out = qc_tools["generate_qc_summary"](str(path))

        assert "### Diagnostic Fragment Analysis" in out
        assert "evaluated only for tandem ms" in out.lower()
        assert "0 ms/ms spectra available" in out.lower()

    def test_qc_nonpositive_declared_ms_level_is_not_treated_as_unknown(
        self,
        qc_tools: dict[str, Callable[..., str]],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        path = tmp_path / "invalid-ms-level.mgf"
        path.write_text(
            "BEGIN IONS\nMSLEVEL=0\n110.0713 10\nEND IONS\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(qc, "DEFAULT_POLICY", SecurityPolicy(allowed_root=tmp_path))

        out = qc_tools["generate_qc_summary"](str(path))

        assert "invalid declared MS level" in out
        assert "MS level was unstated in the input" not in out
        assert "No eligible diagnostic spectra" in out

    def test_qc_unparseable_declared_ms_level_is_disclosed_as_unavailable(
        self,
        qc_tools: dict[str, Callable[..., str]],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        path = tmp_path / "unparseable-ms-level.mgf"
        path.write_text(
            "BEGIN IONS\nMSLEVEL=not-an-integer\n110.0713 10\nEND IONS\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(qc, "DEFAULT_POLICY", SecurityPolicy(allowed_root=tmp_path))

        out = qc_tools["generate_qc_summary"](str(path))

        assert "MS level was absent or unparseable" in out
        assert "evaluated across 1 spectrum" in out.lower()

    def test_qc_diagnostic_fragment_unstated_ms_level(
        self,
        qc_tools: dict[str, Callable[..., str]],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When MS level is unstated, candidate spectra are evaluated and unstated status is disclosed."""
        path = tmp_path / "unstated_levels.mgf"
        content = (
            "BEGIN IONS\n"
            "PEPMASS=194.0804\n"
            "110.0713 50000.0\n"
            "300.0 100000.0\n"
            "END IONS\n"
            "BEGIN IONS\n"
            "PEPMASS=180.0634\n"
            "200.0 10000.0\n"
            "300.0 20000.0\n"
            "END IONS\n"
        )
        path.write_text(content, encoding="utf-8")
        monkeypatch.setattr(qc, "DEFAULT_POLICY", SecurityPolicy(allowed_root=tmp_path))
        out = qc_tools["generate_qc_summary"](str(path))

        assert "### Diagnostic Fragment Analysis" in out
        assert "MS level was absent or unparseable in the input" in out
        assert (
            "|   3  | Histidine immonium        |        110.0713 |        1 |"
            "      50.0% |" in out
        )
