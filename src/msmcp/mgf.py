"""Dependency-free MGF (Mascot Generic Format) reader for MSMCP.

MGF is a widely used interchange format for peak lists, and MassFlow 0.1.x
provides no reader for it (MassFlow targets imzML/zarr imaging data), so MSMCP
owns this small text parser.  It is deliberately strict: a peak line that does
not parse raises :class:`~msmcp.errors.MalformedFileError` rather than being
skipped, because silently dropping peaks would change the science.

Understood fields, all optional except the peak lines themselves:

``PEPMASS``
    Precursor m/z.  ``PEPMASS=<mz> [intensity]``; only the m/z is retained.
``RTINSECONDS`` / ``RTINMINUTES``
    Retention time, normalised to minutes.
``MSLEVEL``
    MS level.  Absent means unknown, which is reported as ``None`` rather than
    guessed.
``TITLE`` / ``CHARGE`` / ``SCANS``
    Metadata that MSMCP does not currently model; accepted and ignored.
"""

from __future__ import annotations

import gzip
import io
from collections.abc import Iterator
from pathlib import Path
from typing import IO, Final, cast

import numpy as np

from msmcp.errors import (
    InaccessiblePathError,
    MalformedFileError,
    UnsupportedFormatError,
)
from msmcp.mzml import Spectrum
from msmcp.security import (
    DEFAULT_POLICY,
    SecurityPolicy,
    resolve_path,
    validate_file_size,
)

__all__ = ["declared_spectrum_count", "iter_spectra", "resolve_mgf_path"]

_BEGIN: Final[str] = "BEGIN IONS"
_END: Final[str] = "END IONS"


def resolve_mgf_path(
    path: str | Path,
    policy: SecurityPolicy = DEFAULT_POLICY,
) -> Path:
    """Validate *path* and return the resolved MGF file to read.

    Applies the filesystem boundary (allowed root + file-size limit) and then
    the access checks: supported extension, existence, regular file, and
    readability.  Each failure raises a distinct error type.
    """
    resolved = resolve_path(path, policy)  # PathEscapeError
    name = resolved.name.lower()
    if not (name.endswith(".mgf") or name.endswith(".mgf.gz")):
        raise UnsupportedFormatError(
            f"'{resolved.suffix}' is not an MGF file.  Expected a .mgf or .mgf.gz file."
        )
    _check_readable(resolved)
    validate_file_size(resolved, policy)  # FileSizeExceededError
    return resolved


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


def _open_text(path: Path) -> IO[str]:
    """Open *path* as text, transparently handling gzip."""
    handle = path.open("rb")
    magic = handle.read(2)
    handle.seek(0)
    if magic == b"\x1f\x8b":
        return io.TextIOWrapper(cast(IO[bytes], gzip.GzipFile(fileobj=handle)))
    return io.TextIOWrapper(handle)


def _to_float_or_none(raw: str | None) -> float | None:
    if raw is None:
        return None
    try:
        return float(raw.split()[0])
    except (TypeError, ValueError, IndexError):
        return None


def _to_int_or_none(raw: str | None) -> int | None:
    if raw is None:
        return None
    try:
        return int(raw.strip())
    except (TypeError, ValueError):
        return None


def _parse_peak_line(line: str, line_number: int) -> tuple[float, float]:
    """Parse one ``<m/z> <intensity>`` peak line."""
    parts = line.split()
    if len(parts) < 2:
        raise MalformedFileError(
            f"MGF peak line {line_number} is not '<m/z> <intensity>': {line!r}"
        )
    try:
        return float(parts[0]), float(parts[1])
    except ValueError as exc:
        raise MalformedFileError(
            f"MGF peak line {line_number} has a non-numeric value: {line!r}"
        ) from exc


def _build_spectrum(
    index: int, fields: dict[str, str], peaks: list[tuple[float, float]]
) -> Spectrum:
    """Turn one accumulated MGF block into a :class:`Spectrum`."""
    if not peaks:
        raise MalformedFileError(
            f"MGF spectrum #{index} (TITLE={fields.get('TITLE', '<none>')!r}) "
            f"contains no peak lines."
        )

    mz = np.array([peak[0] for peak in peaks], dtype=np.float64)
    intensity = np.array([peak[1] for peak in peaks], dtype=np.float64)
    mz.setflags(write=False)
    intensity.setflags(write=False)

    retention_time = _to_float_or_none(fields.get("RTINSECONDS"))
    if retention_time is not None:
        retention_time /= 60.0
    else:
        retention_time = _to_float_or_none(fields.get("RTINMINUTES"))

    return Spectrum(
        index=index,
        ms_level=_to_int_or_none(fields.get("MSLEVEL")),
        retention_time=retention_time,
        precursor_mz=_to_float_or_none(fields.get("PEPMASS")),
        mz=mz,
        intensity=intensity,
    )


def iter_spectra(path: str | Path) -> Iterator[Spectrum]:
    """Stream spectra from *path*, one at a time.

    Raises
    ------
    MalformedFileError
        A block is opened but never closed, or a peak line does not parse.
    """
    resolved = Path(path)
    handle = _open_text(resolved)
    index = 0
    try:
        fields: dict[str, str] = {}
        peaks: list[tuple[float, float]] = []
        inside = False

        for line_number, raw_line in enumerate(handle, start=1):
            line = raw_line.strip()
            if not line or line.startswith(("#", ";", "!")):
                continue

            upper = line.upper()
            if upper == _BEGIN:
                if inside:
                    raise MalformedFileError(
                        f"MGF block at line {line_number} starts before the "
                        f"previous block was closed with '{_END}'."
                    )
                inside = True
                fields = {}
                peaks = []
                continue

            if upper == _END:
                if not inside:
                    raise MalformedFileError(
                        f"MGF file has an '{_END}' at line {line_number} with "
                        f"no matching '{_BEGIN}'."
                    )
                yield _build_spectrum(index, fields, peaks)
                index += 1
                inside = False
                fields = {}
                peaks = []
                continue

            if not inside:
                # Preamble before the first block is legal MGF; ignore it.
                continue

            if "=" in line:
                key, _, value = line.partition("=")
                fields[key.strip().upper()] = value.strip()
            else:
                peaks.append(_parse_peak_line(line, line_number))

        if inside:
            raise MalformedFileError(
                f"MGF file ends inside an unclosed '{_BEGIN}' block."
            )
    finally:
        handle.close()


def declared_spectrum_count(path: str | Path) -> int:
    """Count ``BEGIN IONS`` blocks in *path*.

    MGF has no declared header count, so this is a full scan of the file; it
    exists so callers can report truthful totals before summarising a subset.
    """
    resolved = Path(path)
    handle = _open_text(resolved)
    count = 0
    try:
        for raw_line in handle:
            if raw_line.strip().upper() == _BEGIN:
                count += 1
    finally:
        handle.close()
    return count
