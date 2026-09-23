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
