"""The parsers must survive hostile bytes, not just tidy fixtures.

These are the functions that turn caller-supplied file content into typed
spectra.  Whatever the bytes are, the contract is:

* it parses into spectra whose arrays are finite and the same length, or
* it raises an :class:`~msmcp.errors.MsmcpError`.

Anything else - ``UnicodeDecodeError`` from a Latin-1 MGF, ``gzip.BadGzipFile``
from a truncated ``.gz``, ``binascii.Error`` from a bad payload - escapes the
documented error hierarchy, so the job executor can only report it as an
internal failure.  Hypothesis explores that space far more cheaply than
hand-written fixtures.

The scratch directory is module-scoped on purpose: Hypothesis does not reset
function-scoped fixtures between examples, and a fresh directory per example
would only add file-system churn.
"""

from __future__ import annotations

import gzip
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any, Protocol

import numpy as np
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from msmcp import mgf, mzml
from msmcp.errors import MsmcpError
from msmcp.security import SecurityPolicy

settings.register_profile("fuzz", max_examples=60, deadline=None)
settings.load_profile("fuzz")

FUZZ = st.binary(max_size=2048)
TEXT = st.text(alphabet=st.characters(exclude_categories=["Cs"]))


class _Parser(Protocol):
    def iter_spectra(self, path: Path) -> Iterator[Any]: ...

    def declared_spectrum_count(self, path: Path) -> int: ...


def _assert_spectra_are_sane(spectra: Sequence[Any]) -> None:
    for spectrum in spectra:
        mz = spectrum.mz
        intensity = spectrum.intensity
        assert mz.shape == intensity.shape
        assert np.all(np.isfinite(mz))
        assert np.all(np.isfinite(intensity))


def _parse(module: _Parser, path: Path) -> list[Any]:
    return list(module.iter_spectra(path))


@pytest.fixture(scope="module")
def scratch(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("fuzz")


class TestMgfParser:
    @given(FUZZ)
    def test_arbitrary_bytes_parse_or_raise_msmcp_error(
        self, scratch: Path, data: bytes
    ) -> None:
        path = scratch / "fuzz.mgf"
        path.write_bytes(data)
        try:
            spectra = _parse(mgf, path)
        except MsmcpError:
            return
        _assert_spectra_are_sane(spectra)

    @given(FUZZ)
    def test_corrupt_gzip_is_a_malformed_file(self, scratch: Path, data: bytes) -> None:
        payload = gzip.compress(b"BEGIN IONS\n100.0 10.0\nEND IONS\n" + data)
        corrupt = bytearray(payload)
        corrupt[len(corrupt) // 2] ^= 0xFF
        path = scratch / "fuzz.mgf.gz"
        path.write_bytes(bytes(corrupt))
        try:
            spectra = _parse(mgf, path)
        except MsmcpError:
            return
        _assert_spectra_are_sane(spectra)


class TestMzMLParser:
    @given(FUZZ)
    def test_arbitrary_bytes_parse_or_raise_msmcp_error(
        self, scratch: Path, data: bytes
    ) -> None:
        path = scratch / "fuzz.mzML"
        path.write_bytes(data)
        try:
            spectra = _parse(mzml, path)
        except MsmcpError:
            return
        _assert_spectra_are_sane(spectra)

    @given(TEXT)
    def test_arbitrary_xml_never_crashes_the_decoder(
        self, scratch: Path, data: str
    ) -> None:
        path = scratch / "fuzz_text.mzML"
        path.write_text(f"<mzML><run>{data}</run></mzML>", encoding="utf-8")
        try:
            spectra = _parse(mzml, path)
        except MsmcpError:
            return
        _assert_spectra_are_sane(spectra)

    @given(FUZZ)
    def test_corrupt_gzip_is_a_malformed_file(self, scratch: Path, data: bytes) -> None:
        body = b"<mzML><run><spectrumList count='0'/></run></mzML>" + data
        corrupt = bytearray(gzip.compress(body))
        corrupt[len(corrupt) // 2] ^= 0xFF
        path = scratch / "fuzz.mzML.gz"
        path.write_bytes(bytes(corrupt))
        try:
            spectra = _parse(mzml, path)
        except MsmcpError:
            return
        _assert_spectra_are_sane(spectra)


class TestDeclaredSpectrumCount:
    @pytest.mark.parametrize("module", [mgf, mzml], ids=["mgf", "mzml"])
    @given(FUZZ)
    def test_arbitrary_bytes_never_escape_the_error_hierarchy(
        self, scratch: Path, module: _Parser, data: bytes
    ) -> None:
        path = scratch / "fuzz_count.dat"
        path.write_bytes(data)
        try:
            count = module.declared_spectrum_count(path)
        except MsmcpError:
            return
        assert count >= 0


class TestLibraryReader:
    @given(TEXT)
    def test_arbitrary_msp_text_parses_or_raises(
        self, scratch: Path, data: str
    ) -> None:
        from msmcp.library import get_library_provider

        path = scratch / "fuzz.msp"
        path.write_text(data, encoding="utf-8")
        try:
            provider = get_library_provider(path, SecurityPolicy(allowed_root=scratch))
            spectra = [s for chunk in provider.iter_spectra(100) for s in chunk]
        except MsmcpError:
            return
        _assert_spectra_are_sane(spectra)
