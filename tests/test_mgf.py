"""Tests for the MGF reader.

MGF is one of the two formats MSMCP parses itself (MassFlow ships no MGF
reader), so the parser is tested directly: the metadata it maps, the counts it
reports, gzip handling, and — most importantly — the malformed inputs it
refuses.  A parser that silently skips an unparseable peak line would change
the science without saying so.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from msmcp.errors import (
    InaccessiblePathError,
    MalformedFileError,
    UnsupportedFormatError,
)
from msmcp.mgf import declared_spectrum_count, iter_spectra, resolve_mgf_path
from msmcp.security import (
    FileSizeExceededError,
    PathEscapeError,
    SecurityPolicy,
)


class TestParsing:
    def test_spectrum_count(self, valid_mgf: Path) -> None:
        assert declared_spectrum_count(valid_mgf) == 2

    def test_metadata_is_mapped(self, valid_mgf: Path) -> None:
        first = next(iter_spectra(valid_mgf))
        assert first.index == 0
        assert first.ms_level == 2
        assert first.precursor_mz == pytest.approx(194.0804)
        # RTINSECONDS is normalised to minutes.
        assert first.retention_time == pytest.approx(2.0)
        assert first.coordinate is None

    def test_peak_arrays_and_derived_values(self, valid_mgf: Path) -> None:
        first = next(iter_spectra(valid_mgf))
        assert first.n_peaks == 3
        assert first.tic == pytest.approx(200.0)
        np.testing.assert_allclose(first.mz, [110.0713, 120.0808, 136.0757])
        np.testing.assert_allclose(first.intensity, [40.0, 100.0, 60.0])

    def test_seconds_and_minutes_units(self, valid_mgf: Path) -> None:
        second = list(iter_spectra(valid_mgf))[1]
        assert second.retention_time == pytest.approx(3.5)

    def test_absent_ms_level_is_none_not_guessed(self, valid_mgf: Path) -> None:
        second = list(iter_spectra(valid_mgf))[1]
        assert second.ms_level is None

    def test_peak_arrays_are_read_only(self, valid_mgf: Path) -> None:
        first = next(iter_spectra(valid_mgf))
        assert not first.mz.flags.writeable
        assert not first.intensity.flags.writeable

    def test_gzip_is_transparent(self, gzipped_mgf: Path) -> None:
        spectra = list(iter_spectra(gzipped_mgf))
        assert len(spectra) == 2
        assert spectra[0].n_peaks == 3
        assert declared_spectrum_count(gzipped_mgf) == 2

    def test_preamble_outside_a_block_is_ignored(self, tmp_path: Path) -> None:
        path = tmp_path / "preamble.mgf"
        path.write_text(
            "COM=written by some tool\n"
            "# a comment\n"
            "BEGIN IONS\nPEPMASS=100.0\n120.0 1.0\nEND IONS\n",
            encoding="utf-8",
        )
        spectra = list(iter_spectra(path))
        assert len(spectra) == 1
        assert spectra[0].precursor_mz == pytest.approx(100.0)

    def test_empty_file_has_no_spectra(self, tmp_path: Path) -> None:
        path = tmp_path / "empty.mgf"
        path.write_text("", encoding="utf-8")
        assert declared_spectrum_count(path) == 0
        assert list(iter_spectra(path)) == []


class TestMalformedInput:
    def test_non_numeric_peak_line_is_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "bad_peak.mgf"
        path.write_text(
            "BEGIN IONS\nPEPMASS=100.0\nabc 1.0\nEND IONS\n", encoding="utf-8"
        )
        with pytest.raises(MalformedFileError, match="non-numeric"):
            list(iter_spectra(path))

    def test_short_peak_line_is_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "short_peak.mgf"
        path.write_text("BEGIN IONS\n120.0\nEND IONS\n", encoding="utf-8")
        with pytest.raises(MalformedFileError, match="m/z"):
            list(iter_spectra(path))

    def test_unclosed_block_is_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "unclosed.mgf"
        path.write_text("BEGIN IONS\nPEPMASS=100.0\n120.0 1.0\n", encoding="utf-8")
        with pytest.raises(MalformedFileError, match="unclosed"):
            list(iter_spectra(path))

    def test_nested_begin_is_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "nested.mgf"
        path.write_text(
            "BEGIN IONS\n120.0 1.0\nBEGIN IONS\n130.0 2.0\nEND IONS\n",
            encoding="utf-8",
        )
        with pytest.raises(MalformedFileError, match="before the"):
            list(iter_spectra(path))

    def test_end_without_begin_is_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "stray_end.mgf"
        path.write_text("END IONS\n", encoding="utf-8")
        with pytest.raises(MalformedFileError, match="no matching"):
            list(iter_spectra(path))

    def test_block_without_peaks_is_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "no_peaks.mgf"
        path.write_text("BEGIN IONS\nPEPMASS=100.0\nEND IONS\n", encoding="utf-8")
        with pytest.raises(MalformedFileError, match="no peak lines"):
            list(iter_spectra(path))


class TestPathBoundary:
    def test_resolves_a_valid_path(self, valid_mgf: Path) -> None:
        policy = SecurityPolicy(allowed_root=valid_mgf.parent)
        assert resolve_mgf_path(valid_mgf, policy) == valid_mgf.resolve()

    def test_wrong_extension_is_unsupported(self, tmp_path: Path) -> None:
        path = tmp_path / "data.mzML"
        path.write_text("x", encoding="utf-8")
        policy = SecurityPolicy(allowed_root=tmp_path)
        with pytest.raises(UnsupportedFormatError, match="not an MGF file"):
            resolve_mgf_path(path, policy)

    def test_missing_file(self, tmp_path: Path) -> None:
        policy = SecurityPolicy(allowed_root=tmp_path)
        with pytest.raises(InaccessiblePathError, match="not found"):
            resolve_mgf_path(tmp_path / "missing.mgf", policy)

    def test_directory_is_not_a_file(self, tmp_path: Path) -> None:
        directory = tmp_path / "data.mgf"
        directory.mkdir()
        policy = SecurityPolicy(allowed_root=tmp_path)
        with pytest.raises(InaccessiblePathError, match="Not a regular file"):
            resolve_mgf_path(directory, policy)

    def test_path_escape_is_refused(self, tmp_path: Path, valid_mgf: Path) -> None:
        root = tmp_path / "root"
        root.mkdir()
        policy = SecurityPolicy(allowed_root=root)
        with pytest.raises(PathEscapeError):
            resolve_mgf_path(valid_mgf, policy)

    def test_file_size_limit(self, tmp_path: Path) -> None:
        path = tmp_path / "big.mgf"
        path.write_bytes(b"x" * 1024)
        policy = SecurityPolicy(allowed_root=tmp_path, max_file_size_bytes=10)
        with pytest.raises(FileSizeExceededError):
            resolve_mgf_path(path, policy)
