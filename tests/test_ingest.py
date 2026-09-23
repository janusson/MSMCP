"""Tests for format dispatch (ingestion) and the MassFlow-backed imzML reader.

These are the integration tests for the I/O architecture: whichever path a
file takes — MSMCP's own mzML/MGF readers or MassFlow's imzML reader — the
caller gets the same :class:`~msmcp.mzml.Spectrum`, and provenance records
which backend actually parsed it.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest

from msmcp import ingest, massflow_io
from msmcp.errors import (
    InaccessiblePathError,
    MalformedFileError,
    UnsupportedFormatError,
)
from msmcp.ingest import SUPPORTED_FORMATS, resolve_source
from msmcp.mzml import Spectrum
from msmcp.security import PathEscapeError, SecurityPolicy


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------
class TestDispatch:
    def test_mzml_routes_to_the_msmcp_reader(self, valid_mzml: Path) -> None:
        policy = SecurityPolicy(allowed_root=valid_mzml.parent)
        source = resolve_source(valid_mzml, policy)
        assert (source.format, source.backend) == ("mzML", "msmcp.mzml")
        assert source.declared_spectrum_count() == 3
        assert all(isinstance(s, Spectrum) for s in source.iter_spectra())

    def test_mgf_routes_to_the_msmcp_reader(self, valid_mgf: Path) -> None:
        policy = SecurityPolicy(allowed_root=valid_mgf.parent)
        source = resolve_source(valid_mgf, policy)
        assert (source.format, source.backend) == ("MGF", "msmcp.mgf")
        assert source.declared_spectrum_count() == 2

    def test_imzml_routes_to_massflow(self, imzml_fixture: Path) -> None:
        policy = SecurityPolicy(allowed_root=imzml_fixture.parent)
        source = resolve_source(imzml_fixture, policy)
        assert (source.format, source.backend) == ("imzML", massflow_io.BACKEND)

    @pytest.mark.parametrize(
        "name", ["run.raw", "run.d", "run.wiff", "run.mzXML", "run.parquet", "run"]
    )
    def test_unsupported_formats_are_refused_with_guidance(
        self, tmp_path: Path, name: str
    ) -> None:
        path = tmp_path / name
        path.write_text("x", encoding="utf-8")
        policy = SecurityPolicy(allowed_root=tmp_path)
        with pytest.raises(UnsupportedFormatError) as excinfo:
            resolve_source(path, policy)
        assert ".mzML" in str(excinfo.value) or "Unrecognised" in str(excinfo.value)

    def test_vendor_formats_name_the_vendor(self, tmp_path: Path) -> None:
        path = tmp_path / "run.raw"
        path.write_text("x", encoding="utf-8")
        policy = SecurityPolicy(allowed_root=tmp_path)
        with pytest.raises(UnsupportedFormatError, match="Thermo"):
            resolve_source(path, policy)

    def test_supported_formats_are_documented(self) -> None:
        assert set(SUPPORTED_FORMATS) == {"mzML", "MGF", "imzML"}

    def test_path_escape_is_refused_before_parsing(
        self, tmp_path: Path, valid_mzml: Path
    ) -> None:
        root = tmp_path / "root"
        root.mkdir()
        policy = SecurityPolicy(allowed_root=root)
        with pytest.raises(PathEscapeError):
            resolve_source(valid_mzml, policy)

    def test_policy_defaults_to_the_process_policy(
        self, valid_mgf: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The default policy is looked up at call time, not bound at import."""
        monkeypatch.setattr(
            ingest, "DEFAULT_POLICY", SecurityPolicy(allowed_root=valid_mgf.parent)
        )
        assert resolve_source(valid_mgf).format == "MGF"


# ---------------------------------------------------------------------------
# MassFlow-backed imzML
# ---------------------------------------------------------------------------
class TestMassFlowImzML:
    def test_spectra_are_converted_to_the_canonical_type(
        self, imzml_fixture: Path
    ) -> None:
        spectra = list(massflow_io.iter_spectra(imzml_fixture))
        assert len(spectra) == 2
        assert all(isinstance(s, Spectrum) for s in spectra)
        assert [s.n_peaks for s in spectra] == [3, 3]
        assert [s.tic for s in spectra] == pytest.approx([60.0, 45.0])

    def test_pixel_coordinates_are_preserved(self, imzml_fixture: Path) -> None:
        """Imaging data is identified by pixel, so the coordinate must survive."""
        spectra = list(massflow_io.iter_spectra(imzml_fixture))
        assert {s.coordinate for s in spectra} == {(1, 1, 1), (1, 2, 1)}

    def test_ms_level_comes_from_massflow_metadata(self, imzml_fixture: Path) -> None:
        spectra = list(massflow_io.iter_spectra(imzml_fixture))
        assert all(s.ms_level == 1 for s in spectra)

    def test_arrays_are_read_only(self, imzml_fixture: Path) -> None:
        first = next(iter(massflow_io.iter_spectra(imzml_fixture)))
        assert not first.mz.flags.writeable
        assert not first.intensity.flags.writeable

    def test_declared_count_matches_the_pixels(self, imzml_fixture: Path) -> None:
        assert massflow_io.declared_spectrum_count(imzml_fixture) == 2

    def test_repeated_reads_are_deterministic(self, imzml_fixture: Path) -> None:
        first = list(massflow_io.iter_spectra(imzml_fixture))
        second = list(massflow_io.iter_spectra(imzml_fixture))
        assert [s.tic for s in first] == pytest.approx([s.tic for s in second])

    def test_massflow_version_is_reported(self) -> None:
        assert massflow_io.massflow_version() is not None

    # -- the stdio boundary ------------------------------------------------
    def test_massflow_logging_never_reaches_stdout(self, imzml_fixture: Path) -> None:
        """MassFlow reconfigures the ROOT logger to write to stdout on import.

        On the stdio transport that would corrupt the JSON-RPC framing, so the
        reader must repoint those handlers at stderr.  This is asserted after a
        real read, on the handlers that actually exist in this process.
        """
        list(massflow_io.iter_spectra(imzml_fixture))

        stdout_streams = {s for s in (sys.stdout, sys.__stdout__) if s is not None}
        offenders: list[str] = []
        loggers = [logging.getLogger()]
        loggers += [
            logging.getLogger(name)
            for name in logging.Logger.manager.loggerDict
            if name == "massflow" or name.startswith("massflow.")
        ]
        for logger in loggers:
            if not isinstance(logger, logging.Logger):
                continue
            for handler in logger.handlers:
                if (
                    isinstance(handler, logging.StreamHandler)
                    and handler.stream in stdout_streams
                ):
                    offenders.append(f"{logger.name}: {handler!r}")
        assert not offenders, f"log handlers still writing to stdout: {offenders}"

    # -- failure paths -----------------------------------------------------
    def test_missing_ibd_payload_is_reported(self, imzml_fixture: Path) -> None:
        policy = SecurityPolicy(allowed_root=imzml_fixture.parent)
        imzml_fixture.with_suffix(".ibd").unlink()
        with pytest.raises(InaccessiblePathError, match="paired binary payload"):
            massflow_io.resolve_imzml_path(imzml_fixture, policy)

    def test_ibd_passed_directly_is_refused(self, imzml_fixture: Path) -> None:
        policy = SecurityPolicy(allowed_root=imzml_fixture.parent)
        with pytest.raises(UnsupportedFormatError, match="binary payload"):
            massflow_io.resolve_imzml_path(imzml_fixture.with_suffix(".ibd"), policy)

    def test_wrong_extension_is_unsupported(self, tmp_path: Path) -> None:
        path = tmp_path / "x.mzML"
        path.write_text("x", encoding="utf-8")
        policy = SecurityPolicy(allowed_root=tmp_path)
        with pytest.raises(UnsupportedFormatError, match="not an imzML file"):
            massflow_io.resolve_imzml_path(path, policy)

    def test_ibd_outside_the_allowed_root_is_refused(
        self, tmp_path: Path, imzml_fixture: Path
    ) -> None:
        """The binary payload must be inside the boundary too.

        Points the header at a sibling outside the root by shadowing the
        ``.ibd`` path resolution, and asserts the boundary still holds.
        """
        root = tmp_path / "root"
        root.mkdir()
        header = root / imzml_fixture.name
        header.write_bytes(imzml_fixture.read_bytes())  # .ibd deliberately absent
        policy = SecurityPolicy(allowed_root=root)
        with pytest.raises(InaccessiblePathError, match="paired binary payload"):
            massflow_io.resolve_imzml_path(header, policy)

    def test_corrupt_payload_is_reported_not_guessed(self, imzml_fixture: Path) -> None:
        """A truncated .ibd must raise, never yield plausible-looking spectra."""
        imzml_fixture.with_suffix(".ibd").write_bytes(b"\x00\x01\x02")
        with pytest.raises(MalformedFileError):
            list(massflow_io.iter_spectra(imzml_fixture))
