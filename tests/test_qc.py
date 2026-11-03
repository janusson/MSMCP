"""Tests for the truthful QC summary tool."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest
from conftest import _mzml_xml  # type: ignore[import-not-found]

from msmcp.errors import InaccessiblePathError, MalformedFileError
from msmcp.security import SecurityPolicy
from msmcp.tools import qc


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
        assert "Signal-to-Noise Ratio" in out
        assert "Peak Density" in out

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

    def test_diagnostic_mask_requires_positive_intensity(self) -> None:
        """A zero- or negative-intensity peak at a diagnostic m/z is not a detected ion."""
        mz = np.array([136.0757, 120.0808])
        # zero intensity and negative intensity
        intensity = np.array([0.0, -10.0])
        mask = qc._diagnostic_mask(mz, intensity, tolerance=0.02)
        assert mask == 0

    def test_diagnostic_mask_includes_exact_tolerance_edges(self) -> None:
        mass = 136.0757
        tolerance = 0.02
        mz = np.array([mass - tolerance, mass + tolerance])
        intensity = np.array([1.0, 1.0])

        mask = qc._diagnostic_mask(mz, intensity, tolerance)
        two_ulps_outside = np.nextafter(np.nextafter(mass + tolerance, np.inf), np.inf)
        outside_mask = qc._diagnostic_mask(
            np.array([two_ulps_outside]), np.array([1.0]), tolerance
        )

        assert mask & 1
        assert not outside_mask & 1

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
        assert "applicable only to tandem ms" in out.lower()
        assert "0 ms/ms spectra available" in out.lower()

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
        assert "MS level was unstated in the input" in out
        assert (
            "|   3  | Histidine immonium        |        110.0713 |        1 |"
            "      50.0% |" in out
        )
