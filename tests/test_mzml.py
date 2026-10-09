"""Tests for the mzML reader, I/O tool, and error taxonomy."""

from __future__ import annotations

import base64
import zlib
from collections.abc import Callable
from pathlib import Path

import numpy as np
import pytest

from msmcp.errors import (
    InaccessiblePathError,
    MalformedFileError,
    MissingDependencyError,
    UnsupportedFormatError,
)
from msmcp.mzml import (
    _decode_binary,
    declared_spectrum_count,
    iter_spectra,
    resolve_mzml_path,
)
from msmcp.security import FileSizeExceededError, PathEscapeError, SecurityPolicy
from msmcp.tools import io


# ---------------------------------------------------------------------------
# Parser: binary decoding
# ---------------------------------------------------------------------------
class TestDecodeBinary:
    def test_plain_float64(self) -> None:
        arr = np.array([1.0, 2.5, -3.0], dtype="<f8")
        payload = base64.b64encode(arr.tobytes()).decode()
        assert _decode_binary(
            payload, compression="none", precision="64"
        ) == pytest.approx(arr)

    def test_zlib_float64(self) -> None:
        arr = np.array([1.0, 2.5, -3.0], dtype="<f8")
        payload = base64.b64encode(zlib.compress(arr.tobytes())).decode()
        assert _decode_binary(
            payload, compression="zlib", precision="64"
        ) == pytest.approx(arr)

    def test_float32(self) -> None:
        arr32 = np.array([1.0, -2.0, 3.5], dtype="<f4")
        payload = base64.b64encode(arr32.tobytes()).decode()
        assert _decode_binary(
            payload, compression="none", precision="32"
        ) == pytest.approx(arr32.astype("f8"))

    def test_bad_base64_is_malformed(self) -> None:
        with pytest.raises(MalformedFileError, match="base64"):
            _decode_binary("!!!not-base64!!!", compression="none", precision="64")

    def test_numpress_missing_dependency(self) -> None:
        with pytest.raises(MissingDependencyError, match="pynumpress"):
            _decode_binary("", compression="numpress", precision="64")


# ---------------------------------------------------------------------------
# Parser: spectrum iteration and counts
# ---------------------------------------------------------------------------
class TestMzMLParser:
    def test_declared_spectrum_count(self, valid_mzml: Path) -> None:
        assert declared_spectrum_count(valid_mzml) == 3

    def test_spectrum_tic_and_counts(self, valid_mzml: Path) -> None:
        spectra = list(iter_spectra(valid_mzml))
        assert len(spectra) == 3
        assert [s.tic for s in spectra] == pytest.approx([60.0, 10.0, 10.0])
        assert [s.n_peaks for s in spectra] == [3, 2, 4]

    def test_spectrum_metadata(self, valid_mzml: Path) -> None:
        first = next(iter_spectra(valid_mzml))
        assert first.index == 0
        assert first.ms_level == 1
        assert first.precursor_mz == pytest.approx(300.0)
        assert first.retention_time == pytest.approx(0.0)

    def test_truncated_file_is_malformed(self, truncated_mzml: Path) -> None:
        with pytest.raises(MalformedFileError):
            list(iter_spectra(truncated_mzml))


# ---------------------------------------------------------------------------
# Error taxonomy at the access boundary
# ---------------------------------------------------------------------------
class TestErrorTaxonomy:
    def test_missing_file(self, tmp_path: Path) -> None:
        policy = SecurityPolicy(allowed_root=tmp_path)
        with pytest.raises(InaccessiblePathError, match="not found"):
            resolve_mzml_path(tmp_path / "missing.mzML", policy)

    def test_directory_is_not_a_file(self, tmp_path: Path) -> None:
        directory = tmp_path / "data.mzML"
        directory.mkdir()
        policy = SecurityPolicy(allowed_root=tmp_path)
        with pytest.raises(InaccessiblePathError, match="Not a regular file"):
            resolve_mzml_path(directory, policy)

    @pytest.mark.parametrize("ext", [".mgf", ".raw", ".d", ".txt"])
    def test_unsupported_formats(self, tmp_path: Path, ext: str) -> None:
        path = tmp_path / f"data{ext}"
        path.write_text("x")
        policy = SecurityPolicy(allowed_root=tmp_path)
        with pytest.raises(UnsupportedFormatError):
            resolve_mzml_path(path, policy)


# ---------------------------------------------------------------------------
# Security boundaries applied to the I/O path
# ---------------------------------------------------------------------------
class TestSecurityBoundary:
    def test_path_escape(self, tmp_path: Path, valid_mzml: Path) -> None:
        root = tmp_path / "root"
        root.mkdir()
        policy = SecurityPolicy(allowed_root=root)
        with pytest.raises(PathEscapeError):
            resolve_mzml_path(valid_mzml, policy)

    def test_file_size_limit(self, tmp_path: Path) -> None:
        big = tmp_path / "big.mzML"
        big.write_bytes(b"x" * 1024)
        policy = SecurityPolicy(allowed_root=tmp_path, max_file_size_bytes=10)
        with pytest.raises(FileSizeExceededError):
            resolve_mzml_path(big, policy)


# ---------------------------------------------------------------------------
# Tool-level behaviour
# ---------------------------------------------------------------------------
class TestLoadMzMLSummary:
    def test_summary(
        self,
        io_tools: dict[str, Callable[..., str]],
        valid_mzml: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            io, "DEFAULT_POLICY", SecurityPolicy(allowed_root=valid_mzml.parent)
        )
        out = io_tools["load_mzml_summary"](str(valid_mzml))
        assert "valid.mzML" in out
        assert "Summarised 3 of 3 total spectra." in out
        assert "TIC:" in out

    def test_summary_respects_max_spectra(
        self,
        io_tools: dict[str, Callable[..., str]],
        valid_mzml: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            io, "DEFAULT_POLICY", SecurityPolicy(allowed_root=valid_mzml.parent)
        )
        out = io_tools["load_mzml_summary"](str(valid_mzml), max_spectra=2)
        assert "Summarised 2 of 3 total spectra." in out

    def test_missing_file_raises(
        self,
        io_tools: dict[str, Callable[..., str]],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(io, "DEFAULT_POLICY", SecurityPolicy(allowed_root=tmp_path))
        with pytest.raises(InaccessiblePathError):
            io_tools["load_mzml_summary"](str(tmp_path / "missing.mzML"))

    def test_truncated_file_raises(
        self,
        io_tools: dict[str, Callable[..., str]],
        truncated_mzml: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            io, "DEFAULT_POLICY", SecurityPolicy(allowed_root=truncated_mzml.parent)
        )
        with pytest.raises(MalformedFileError):
            io_tools["load_mzml_summary"](str(truncated_mzml))


# ---------------------------------------------------------------------------
# Zero-length arrays: real acquisitions contain legally empty scans
# ---------------------------------------------------------------------------
class TestEmptyBinaryArrays:
    """An empty array is data (no peaks), not a missing payload.

    ``COE001_16ppm_5uL.mzML`` (Bruker maXis QTOF) carries 4,990 zero-length
    arrays across 2,495 of its 9,286 spectra, written as ``encodedLength="0"``
    with a self-closing ``<binary/>``.  The reader treated every one of them as
    corruption and aborted the whole file at its third spectrum, so a real
    acquisition could not be read at all.
    """

    def test_zero_length_array_yields_an_empty_spectrum(
        self, mzml_with_empty_scan: Path
    ) -> None:
        """An empty scan parses, and does not stop the spectra after it."""
        spectra = list(iter_spectra(mzml_with_empty_scan))

        assert len(spectra) == 3
        empty = spectra[1]
        assert empty.ms_level == 2
        assert empty.mz.size == 0
        assert empty.intensity.size == 0
        assert empty.precursor_mz == pytest.approx(89.50771332)
        # The reader keeps going: the scan after the empty one still arrives.
        assert spectra[2].mz.size == 2
        assert spectra[2].precursor_mz == pytest.approx(200.0)

    def test_empty_arrays_are_read_only(self, mzml_with_empty_scan: Path) -> None:
        """Empty arrays share the immutability of every other decoded array."""
        empty = list(iter_spectra(mzml_with_empty_scan))[1]
        assert not empty.mz.flags.writeable
        assert not empty.intensity.flags.writeable

    def test_absent_binary_element_is_an_empty_array(
        self, mzml_with_absent_binary: Path
    ) -> None:
        """``<binary>`` has ``minOccurs="0"``: omitting it still means no peaks."""
        spectra = list(iter_spectra(mzml_with_absent_binary))
        assert spectra[0].mz.size == 0
        assert spectra[0].intensity.size == 0

    def test_declared_peaks_with_an_empty_payload_is_malformed(
        self, mzml_declares_peaks_with_empty_payload: Path
    ) -> None:
        """Emptiness is legal; a declaration that contradicts it is not.

        Strictness is kept where it earns its keep: a file that says it has
        three peaks and carries none is corruption, exactly as a ``Num Peaks``
        header that disagrees with the peak lines is in the MSP reader.
        """
        with pytest.raises(MalformedFileError, match="declares"):
            list(iter_spectra(mzml_declares_peaks_with_empty_payload))


# ---------------------------------------------------------------------------
# A declaration the arrays cannot supply: reported, not refused
# ---------------------------------------------------------------------------
class TestDeclaredLengthMismatch:
    """A declaration the arrays cannot supply is reported, not refused.

    Emptiness in a spectrum that declares peaks is unambiguous corruption and
    still raises.  A payload that decodes to *fewer* values than the spectrum
    claims is a different case: it may be a writer's bookkeeping rather than
    corruption, and refusing the file would make a released server reject data it
    accepts today.  It is reported once per file instead — visible without the
    breakage — and the decoded arrays, which are what every downstream tool
    actually uses, are yielded as they are.
    """

    def test_a_short_payload_is_read_and_reported(
        self,
        mzml_declares_more_than_it_supplies: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The file is read; the inconsistency is named once, at WARNING."""
        with caplog.at_level("WARNING", logger="msmcp.mzml"):
            spectra = list(iter_spectra(mzml_declares_more_than_it_supplies))

        assert len(spectra) == 1
        assert spectra[0].mz.size == 2  # read as decoded, not padded or refused
        assert spectra[0].intensity.size == 2

        assert "declare a peak count" in caplog.text
        assert "1 spectrum" in caplog.text

    def test_a_consistent_file_is_not_reported(
        self, valid_mzml: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """No warning when the declaration and the arrays agree."""
        with caplog.at_level("WARNING", logger="msmcp.mzml"):
            list(iter_spectra(valid_mzml))

        assert caplog.text == ""

    def test_the_report_is_one_line_per_file_not_per_spectrum(
        self,
        mzml_two_short_payloads: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Aggregated on purpose: a systematically off writer must not spam."""
        with caplog.at_level("WARNING", logger="msmcp.mzml"):
            list(iter_spectra(mzml_two_short_payloads))

        warnings = [r for r in caplog.records if r.levelno >= 30]
        assert len(warnings) == 1
        assert "2 spectrum" in warnings[0].getMessage()
