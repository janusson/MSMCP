"""Dependency-free mzML reader for MSMCP.

MassFlow 0.1.x targets imzML / zarr imaging data and ships no mzML reader, so
MSMCP parses mzML itself with the standard library.  This is the deliberate
fallback for a format the MassFlow layer does not cover - not a duplicate of
it: :mod:`msmcp.massflow_io` reads imzML through MassFlow, and
:mod:`msmcp.ingest` dispatches a path to whichever of the two applies.  The
reader understands the subset of the format required for truthful summaries:

- ``<spectrumList count="...">`` — the declared number of spectra;
- each ``<spectrum>`` — MS level, retention time, precursor m/z;
- each ``<binaryDataArray>`` — base64-encoded m/z and intensity arrays
  (64- or 32-bit little-endian floats, optionally zlib-compressed).

Standard mzML needs no external dependency.  MS-Numpress-compressed arrays are
detected and reported as :class:`~msmcp.errors.MissingDependencyError` because
they require the optional ``pynumpress`` package.
"""

from __future__ import annotations

import base64
import binascii
import gzip
import importlib
import xml.etree.ElementTree as ET
import zlib
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import IO, cast

import numpy as np

from msmcp.errors import (
    InaccessiblePathError,
    MalformedFileError,
    MissingDependencyError,
    UnsupportedFormatError,
)
from msmcp.security import (
    DEFAULT_POLICY,
    SecurityPolicy,
    resolve_path,
    validate_file_size,
)

__all__ = [
    "Spectrum",
    "declared_spectrum_count",
    "iter_spectra",
    "resolve_mzml_path",
]

_ACC_MZ_ARRAY = "MS:1000514"
_ACC_INTENSITY_ARRAY = "MS:1000515"
_ACC_32BIT_FLOAT = "MS:1000521"
_ACC_64BIT_FLOAT = "MS:1000523"
_ACC_ZLIB_COMPRESSION = "MS:1000574"
_ACC_NO_COMPRESSION = "MS:1000576"
_ACC_MS_LEVEL = "MS:1000511"
_ACC_SCAN_START_TIME = "MS:1000016"
_ACC_SELECTED_ION_MZ = "MS:1000744"

_NUMPRESS_ACCESSIONS = frozenset({"MS:1002312", "MS:1002313", "MS:1002314"})


@dataclass(frozen=True, slots=True)
class Spectrum:
    """A single parsed spectrum.

    Immutable by construction: the instance fields cannot be reassigned and the
    ``mz`` / ``intensity`` buffers are read-only, so a parsed spectrum can be
    handed to several tools (and stored in the data-reference registry) without
    any of them being able to alter what the others see.

    ``coordinate`` is populated only for imaging sources (imzML, via MassFlow),
    where a spectrum is identified by its ``(x, y, z)`` pixel rather than by a
    scan index; for LC-MS sources it stays ``None``.
    """

    index: int | None
    ms_level: int | None
    retention_time: float | None  # minutes
    precursor_mz: float | None
    mz: np.ndarray
    intensity: np.ndarray
    coordinate: tuple[int, int, int] | None = None

    @property
    def tic(self) -> float:
        """Total ion current: the exact sum of the intensity array."""
        return float(np.sum(self.intensity, dtype=np.float64))

    @property
    def n_peaks(self) -> int:
        """Number of peaks in the spectrum."""
        return int(self.mz.size)


# ---------------------------------------------------------------------------
# Path resolution + access boundary
# ---------------------------------------------------------------------------
def resolve_mzml_path(
    path: str | Path,
    policy: SecurityPolicy = DEFAULT_POLICY,
) -> Path:
    """Validate *path* and return the resolved mzML file to read.

    Applies the filesystem boundary (allowed root + file-size limit) and then
    the truthful access checks: supported extension, existence, regular file,
    and readability.  Each failure raises a distinct error type.
    """
    resolved = resolve_path(path, policy)  # PathEscapeError
    _check_supported_format(resolved)  # UnsupportedFormatError
    _check_readable(resolved)  # InaccessiblePathError
    validate_file_size(resolved, policy)  # FileSizeExceededError
    return resolved


def _check_supported_format(path: Path) -> None:
    name = path.name.lower()

    if name.endswith(".mzml") or name.endswith(".mzml.gz"):
        return

    if name.endswith(".raw") or name.endswith(".d"):
        raise UnsupportedFormatError(
            f"Unsupported vendor format '{path.suffix}'. Thermo .raw and "
            f"Agilent/Bruker .d files cannot be parsed directly; convert to "
            f".mzML with ProteoWizard MSConvert and retry."
        )

    if name.endswith(".imzml"):
        raise UnsupportedFormatError(
            "Unsupported format '.imzML' for the mzML reader: imaging MS data "
            "is read through MassFlow.  Use the imaging-aware ingestion path "
            "(MassFlow) instead of the mzML reader."
        )

    if name.endswith(".mgf"):
        raise UnsupportedFormatError(
            "Unsupported format '.mgf' for the mzML reader: use the MGF "
            "reader instead (the ingestion layer selects it automatically)."
        )

    raise UnsupportedFormatError(
        f"Unrecognised file extension '{path.suffix}'. The mzML reader accepts "
        f"only .mzML and .mzML.gz."
    )


def _check_readable(path: Path) -> None:
    try:
        path.stat()
    except FileNotFoundError as exc:
        raise InaccessiblePathError(f"File not found: '{path}'") from exc
    except PermissionError as exc:
        raise InaccessiblePathError(
            f"Permission denied reading '{path}': {exc}"
        ) from exc
    except OSError as exc:
        raise InaccessiblePathError(f"Cannot access '{path}': {exc}") from exc

    if not path.is_file():
        raise InaccessiblePathError(f"Not a regular file: '{path}'")

    if not _is_readable(path):
        raise InaccessiblePathError(f"File is not readable: '{path}'")


def _is_readable(path: Path) -> bool:
    try:
        with path.open("rb"):
            return True
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Low-level XML / binary decoding
# ---------------------------------------------------------------------------
def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child(elem: ET.Element, name: str) -> ET.Element | None:
    for child in elem:
        if _local(child.tag) == name:
            return child
    return None


def _to_int_or_none(raw: str | None) -> int | None:
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _to_float_or_none(raw: str | None) -> float | None:
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _open_binary(path: Path) -> IO[bytes]:
    """Open *path* for binary reading, transparently handling gzip."""
    handle = path.open("rb")
    magic = handle.read(2)
    handle.seek(0)
    if magic == b"\x1f\x8b":
        return cast(IO[bytes], gzip.GzipFile(fileobj=handle))
    return handle


def _decode_binary(text: str, *, compression: str, precision: str) -> np.ndarray:
    """Decode a ``<binary>`` payload into a float64 array."""
    if compression == "numpress":
        try:
            importlib.import_module("pynumpress")
        except ImportError as exc:
            raise MissingDependencyError(
                "This mzML file uses MS-Numpress compression, which requires "
                "the optional 'pynumpress' package. Install it and retry."
            ) from exc
        raise UnsupportedFormatError(
            "MS-Numpress decoding is not implemented in this milestone."
        )

    clean = "".join(text.split())
    try:
        raw = base64.b64decode(clean, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise MalformedFileError("mzML <binary> payload is not valid base64.") from exc

    if compression == "zlib":
        try:
            raw = zlib.decompress(raw)
        except zlib.error as exc:
            raise MalformedFileError(
                "mzML <binary> payload failed zlib decompression."
            ) from exc

    dtype = np.dtype("<f8") if precision == "64" else np.dtype("<f4")
    if len(raw) % dtype.itemsize:
        raise MalformedFileError(
            "mzML <binary> payload length is not a multiple of the element size."
        )
    array = np.frombuffer(raw, dtype=dtype).astype(np.float64, copy=False)
    # Peaks are read-only: a decoded array is shared with every consumer of the
    # spectrum, so no caller may mutate it in place.
    array.setflags(write=False)
    return array


def _binary_array_cv_accessions(elem: ET.Element) -> set[str]:
    return {
        child.get("accession", "")
        for child in elem
        if _local(child.tag) == "cvParam" and child.get("accession")
    }


def _parse_binary_data_array(elem: ET.Element) -> tuple[np.ndarray, str]:
    accessions = _binary_array_cv_accessions(elem)

    is_mz = _ACC_MZ_ARRAY in accessions
    is_intensity = _ACC_INTENSITY_ARRAY in accessions
    if is_mz == is_intensity:
        raise MalformedFileError(
            "A binaryDataArray must be exactly one of an m/z or intensity array."
        )
    kind = "mz" if is_mz else "intensity"

    if _ACC_64BIT_FLOAT in accessions:
        precision = "64"
    elif _ACC_32BIT_FLOAT in accessions:
        precision = "32"
    else:
        precision = "64"  # common default when a producer omits the cvParam

    if accessions & _NUMPRESS_ACCESSIONS:
        compression = "numpress"
    elif _ACC_ZLIB_COMPRESSION in accessions:
        compression = "zlib"
    else:
        compression = "none"

    binary = _child(elem, "binary")
    if binary is None or binary.text is None:
        raise MalformedFileError("A binaryDataArray is missing its <binary> payload.")

    return _decode_binary(
        binary.text, compression=compression, precision=precision
    ), kind


def _extract_scan_start_time(elem: ET.Element) -> float | None:
    for scan in elem:
        if _local(scan.tag) != "scan":
            continue
        for cv in scan:
            if (
                _local(cv.tag) == "cvParam"
                and cv.get("accession") == _ACC_SCAN_START_TIME
            ):
                value = _to_float_or_none(cv.get("value"))
                if value is None:
                    return None
                unit = (cv.get("unitName") or "").lower()
                # Normalise to minutes for the summary; the mzML default is seconds.
                return value / 60.0 if "second" in unit else value
    return None


def _extract_precursor_mz(elem: ET.Element) -> float | None:
    for precursor in elem:
        if _local(precursor.tag) != "precursor":
            continue
        for selected_ion_list in precursor:
            if _local(selected_ion_list.tag) != "selectedIonList":
                continue
            for selected_ion in selected_ion_list:
                if _local(selected_ion.tag) != "selectedIon":
                    continue
                for cv in selected_ion:
                    if (
                        _local(cv.tag) == "cvParam"
                        and cv.get("accession") == _ACC_SELECTED_ION_MZ
                    ):
                        return _to_float_or_none(cv.get("value"))
    return None


def _parse_spectrum(elem: ET.Element) -> Spectrum:
    index = _to_int_or_none(elem.get("index"))
    ms_level: int | None = None
    retention_time: float | None = None
    precursor_mz: float | None = None

    for child in elem:
        tag = _local(child.tag)
        if tag == "cvParam" and child.get("accession") == _ACC_MS_LEVEL:
            ms_level = _to_int_or_none(child.get("value"))
        elif tag == "scanList":
            retention_time = _extract_scan_start_time(child)
        elif tag == "precursorList":
            precursor_mz = _extract_precursor_mz(child)

    binary_data_array_list = _child(elem, "binaryDataArrayList")
    if binary_data_array_list is None:
        raise MalformedFileError("A spectrum is missing its binaryDataArrayList.")

    mz: np.ndarray | None = None
    intensity: np.ndarray | None = None
    for binary_data_array in binary_data_array_list:
        if _local(binary_data_array.tag) != "binaryDataArray":
            continue
        arr, kind = _parse_binary_data_array(binary_data_array)
        if kind == "mz":
            mz = arr
        elif kind == "intensity":
            intensity = arr

    if mz is None or intensity is None:
        raise MalformedFileError(
            "A spectrum is missing an m/z or intensity binary array."
        )

    return Spectrum(
        index=index,
        ms_level=ms_level,
        retention_time=retention_time,
        precursor_mz=precursor_mz,
        mz=mz,
        intensity=intensity,
    )


# ---------------------------------------------------------------------------
# Public iteration / counting
# ---------------------------------------------------------------------------
def iter_spectra(path: str | Path) -> Iterator[Spectrum]:
    """Stream spectra from *path*, one at a time."""
    resolved = Path(path)
    handle = _open_binary(resolved)
    try:
        for event, elem in ET.iterparse(handle, events=("end",)):
            if event == "end" and _local(elem.tag) == "spectrum":
                try:
                    yield _parse_spectrum(elem)
                finally:
                    elem.clear()
    except ET.ParseError as exc:
        raise MalformedFileError(f"Malformed mzML: {exc}") from exc
    finally:
        handle.close()


def declared_spectrum_count(path: str | Path) -> int:
    """Return the ``spectrumList/@count`` declared in *path*."""
    resolved = Path(path)
    handle = _open_binary(resolved)
    try:
        for event, elem in ET.iterparse(handle, events=("start",)):
            if event == "start" and _local(elem.tag) == "spectrumList":
                raw = elem.get("count")
                if raw is None:
                    return 0
                try:
                    return int(raw)
                except ValueError as exc:
                    raise MalformedFileError(
                        "spectrumList/@count is not an integer."
                    ) from exc
        raise MalformedFileError("The file contains no spectrumList element.")
    except ET.ParseError as exc:
        raise MalformedFileError(f"Malformed mzML: {exc}") from exc
    finally:
        handle.close()
