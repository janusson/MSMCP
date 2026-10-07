"""Real spectral-library readers, confined by the acquisition security boundary.

``search_library`` used to treat its ``database_file`` as a seed for a
synthetic in-memory library, so a path was never resolved and nothing validated
it.  This module is the missing other half: it reads a spectral library that the
user already has on disk, in a format they already have it in.

The first (and, for now, only) reader is **MSP / NIST-style text**, the
interchange format most commercial and public libraries ship as.  It is
deliberately strict: a peak line that does not parse raises
:class:`~msmcp.errors.MalformedFileError` rather than being skipped, and a
``Num Peaks`` header that disagrees with the peak lines read is an error, because
silently dropping or inventing peaks would change the science.

Every path crosses the same :class:`~msmcp.security.SecurityPolicy` as
acquisitions — allowed root (symlinks resolved), file-size limit — and is opened
**read-only** (mode ``"rb"``); the reader never needs, or takes, write access to
a library file.  That is the security criterion the audit attaches to this item:
the moment a provider opens ``database_file``, an unvalidated path would be
arbitrary file read from an MCP tool.

The provider shape mirrors :class:`~msmcp.models.embeddings.SpectralEmbedder`
and :class:`~msmcp.execution.executor.JobExecutor` — the third use of the same
pattern, as ARCHITECTURE.md plans it — so the scan is written against
:class:`LibraryProvider` and a future SQLite peak store can be added without
touching the search pipeline.
"""

from __future__ import annotations

import gzip
import io
import re
from abc import ABC, abstractmethod
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import IO, Final, cast

from msmcp.errors import (
    InaccessiblePathError,
    MalformedFileError,
    UnsupportedFormatError,
)
from msmcp.provenance import file_digest
from msmcp.security import SecurityPolicy, resolve_path, validate_file_size

__all__ = [
    "MSP_SUFFIXES",
    "LibraryInfo",
    "LibraryProvider",
    "LibrarySpectrum",
    "MSPLibraryProvider",
    "get_library_provider",
    "is_supported_library_path",
    "resolve_library_path",
]

MSP_SUFFIXES: Final[tuple[str, ...]] = (".msp", ".msp.gz")
"""File suffixes read as MSP / NIST-style text libraries."""

_COUNT_CHUNK: Final[int] = 10_000
"""Chunk size used when counting spectra for :meth:`LibraryProvider.describe`."""

# A header line is ``Key: value`` where the key starts with a letter and may
# contain spaces (``Num Peaks``), underscores (``Precursor_mz``) or a hash
# (``DB#``).  A peak line never matches: it begins with a digit, sign or dot.
_HEADER_RE: Final[re.Pattern[str]] = re.compile(
    r"^([A-Za-z][A-Za-z0-9 _#/.\-]*):\s?(.*)$"
)
_QUOTED_RE: Final[re.Pattern[str]] = re.compile(r'"[^"]*"')

# Normalised (letters/digits only, uppercased) header keys the reader interprets.
_NAME_KEYS: Final[frozenset[str]] = frozenset({"NAME", "COMPOUND"})
_FORMULA_KEYS: Final[frozenset[str]] = frozenset({"FORMULA", "MF", "MOLFORMULA"})
_PRECURSOR_KEYS: Final[frozenset[str]] = frozenset({"PRECURSORMZ", "PMZ"})
_NUM_PEAKS_KEYS: Final[frozenset[str]] = frozenset({"NUMPEAKS"})


def _normalise_key(raw: str) -> str:
    """Collapse a header key to ``[A-Z0-9]`` so spellings agree."""
    return re.sub(r"[^A-Z0-9]", "", raw.upper())


def _to_float_or_none(raw: str | None) -> float | None:
    """Parse *raw* as a float, or return ``None`` when it is absent/invalid."""
    if raw is None:
        return None
    try:
        return float(raw.strip())
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class LibrarySpectrum:
    """One library entry: a compound name plus a peak list.

    ``peaks`` is a tuple of ``(m/z, intensity)`` float pairs — the exact value
    the scorer consumes — so a library spectrum never needs a second
    representation.  Metadata that MSMCP does not model (ion mode, collision
    energy, InChIKey, ...) is preserved verbatim in :attr:`metadata` rather than
    dropped, so a later reader can use it without a re-parse.
    """

    index: int
    compound_name: str
    precursor_mz: float | None
    formula: str | None
    peaks: tuple[tuple[float, float], ...]
    metadata: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class LibraryInfo:
    """What a provider can say about a library without returning its spectra."""

    path: str
    name: str
    format: str
    version: str | None
    n_spectra: int
    digest: str | None
    size_bytes: int | None


# ---------------------------------------------------------------------------
# The provider interface
# ---------------------------------------------------------------------------
class LibraryProvider(ABC):
    """A spectral library addressable one chunk of spectra at a time.

    The library file is supplied already validated by
    :func:`resolve_library_path`; a provider only ever reads it.
    """

    format: str = "unknown"

    def __init__(self, path: Path) -> None:
        self._path = path

    @property
    def path(self) -> Path:
        """The validated, symlink-resolved library file."""
        return self._path

    @abstractmethod
    def describe(self) -> LibraryInfo:
        """Return this library's identity and size."""

    @abstractmethod
    def iter_spectra(self, chunk_size: int = 2000) -> Iterator[list[LibrarySpectrum]]:
        """Stream spectra in chunks; each provider may be iterated repeatedly."""


# ---------------------------------------------------------------------------
# MSP / NIST-style text
# ---------------------------------------------------------------------------
def _open_text(path: Path) -> IO[str]:
    """Open *path* read-only as text, transparently handling gzip."""
    handle = path.open("rb")
    magic = handle.read(2)
    handle.seek(0)
    if magic == b"\x1f\x8b":
        return io.TextIOWrapper(cast(IO[bytes], gzip.GzipFile(fileobj=handle)))
    return io.TextIOWrapper(handle)


def _parse_peak_line(line: str, line_number: int) -> list[tuple[float, float]]:
    """Parse one MSP peak line into one or more ``(m/z, intensity)`` pairs.

    A line may carry several ``;``-separated peaks, and an optional trailing
    annotation in double quotes.  A group without two numeric fields is a
    malformed line, not a peak to skip.
    """
    cleaned = _QUOTED_RE.sub(" ", line)
    pairs: list[tuple[float, float]] = []
    for group in cleaned.split(";"):
        if not group.strip():
            continue
        parts = group.split()
        if len(parts) < 2:
            raise MalformedFileError(
                f"MSP peak line {line_number} is not '<m/z> <intensity>': {line!r}"
            )
        try:
            pairs.append((float(parts[0]), float(parts[1])))
        except ValueError as exc:
            raise MalformedFileError(
                f"MSP peak line {line_number} has a non-numeric value: {line!r}"
            ) from exc
    return pairs


def _build_spectrum(
    index: int,
    line_number: int,
    fields: dict[str, str],
    peaks: list[tuple[float, float]],
) -> LibrarySpectrum:
    """Turn one accumulated MSP block into a :class:`LibrarySpectrum`."""
    name = _first(fields, _NAME_KEYS) or ""
    if not peaks:
        raise MalformedFileError(
            f"MSP spectrum #{index} ({name or '<unnamed>'}) contains no peak lines."
        )

    declared_raw = _first(fields, _NUM_PEAKS_KEYS)
    if declared_raw is not None:
        try:
            declared = int(declared_raw.strip())
        except (TypeError, ValueError) as exc:
            raise MalformedFileError(
                f"MSP spectrum #{index} has a non-integer Num Peaks header: "
                f"{declared_raw!r}"
            ) from exc
        if declared != len(peaks):
            raise MalformedFileError(
                f"MSP spectrum #{index} ({name or '<unnamed>'}) declares "
                f"Num Peaks: {declared} but {len(peaks)} peak lines were read "
                f"(ended at line {line_number})."
            )

    return LibrarySpectrum(
        index=index,
        compound_name=name,
        precursor_mz=_to_float_or_none(_first(fields, _PRECURSOR_KEYS)),
        formula=_first(fields, _FORMULA_KEYS),
        peaks=tuple(peaks),
        metadata=MappingProxyType(dict(fields)),
    )


def _first(fields: dict[str, str], keys: frozenset[str]) -> str | None:
    """Return the first value whose normalised key is in *keys*."""
    for key in keys:
        if key in fields:
            return fields[key]
    return None


def iter_msp_spectra(path: Path) -> Iterator[LibrarySpectrum]:
    """Stream :class:`LibrarySpectrum` objects from an MSP file.

    Records are separated by a blank line or by the next ``Name:`` line.  The
    reader raises on any malformed peak line, mismatched ``Num Peaks`` header or
    a record with no peaks — it never returns a partially-parsed library as if
    it were complete.
    """
    handle = _open_text(path)
    fields: dict[str, str] = {}
    peaks: list[tuple[float, float]] = []
    have_content = False
    index = 0
    line_number = 0
    try:
        for raw_line in handle:
            line_number += 1
            line = raw_line.strip()
            if not line:
                if have_content:
                    yield _build_spectrum(index, line_number, fields, peaks)
                    index += 1
                    fields, peaks, have_content = {}, [], False
                continue
            if line.startswith(("#", ";", "!")):
                continue

            match = _HEADER_RE.match(line)
            if match is None:
                peaks.extend(_parse_peak_line(line, line_number))
                have_content = True
                continue

            key = _normalise_key(match.group(1))
            if key in _NAME_KEYS and have_content:
                # A new record without a separating blank line.
                yield _build_spectrum(index, line_number, fields, peaks)
                index += 1
                fields, peaks = {}, []
            fields[key] = match.group(2).strip()
            have_content = True

        if have_content:
            yield _build_spectrum(index, line_number, fields, peaks)
    finally:
        handle.close()


class MSPLibraryProvider(LibraryProvider):
    """Read a spectral library stored as MSP / NIST-style text."""

    format = "MSP"

    def describe(self) -> LibraryInfo:
        """Count the library's spectra and digest the file it came from."""
        n_spectra = sum(
            len(chunk) for chunk in self.iter_spectra(_COUNT_CHUNK)
        )
        try:
            size = self._path.stat().st_size
        except OSError:
            size = None
        return LibraryInfo(
            path=str(self._path),
            name=self._path.name,
            format=self.format,
            version="NIST/MSP text",
            n_spectra=n_spectra,
            digest=file_digest(self._path),
            size_bytes=size,
        )

    def iter_spectra(self, chunk_size: int = 2000) -> Iterator[list[LibrarySpectrum]]:
        """Yield the library's spectra in chunks of at most *chunk_size*."""
        if chunk_size < 1:
            raise ValueError(f"chunk_size must be positive, got {chunk_size!r}")
        chunk: list[LibrarySpectrum] = []
        for spectrum in iter_msp_spectra(self._path):
            chunk.append(spectrum)
            if len(chunk) >= chunk_size:
                yield chunk
                chunk = []
        if chunk:
            yield chunk


# ---------------------------------------------------------------------------
# Path resolution and the provider registry
# ---------------------------------------------------------------------------
def is_supported_library_path(path: str | Path) -> bool:
    """Whether *path*'s suffix names a library format MSMCP can read."""
    return _provider_class_for(Path(path).name) is not None


def _provider_class_for(name: str) -> type[LibraryProvider] | None:
    """Return the provider class for *name*'s suffix, or ``None``."""
    lowered = name.lower()
    if lowered.endswith(MSP_SUFFIXES):
        return MSPLibraryProvider
    return None


def resolve_library_path(
    path: str | Path,
    policy: SecurityPolicy,
) -> Path:
    """Validate a library *path* against *policy* and return it resolved.

    Applies the same boundary as acquisitions — allowed root (symlinks
    resolved), supported format, existence/readability, file-size limit — and
    raises the matching error type.  The returned path is opened read-only by
    the provider; MSMCP never writes to a library file.
    """
    resolved = resolve_path(path, policy)  # PathEscapeError
    if not is_supported_library_path(resolved):
        raise UnsupportedFormatError(
            f"'{resolved.suffix}' is not a supported spectral-library format. "
            f"MSMCP reads MSP/NIST-style text ({', '.join(MSP_SUFFIXES)}); the "
            f"path was not opened."
        )
    _check_readable(resolved)  # InaccessiblePathError
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


def get_library_provider(path: str | Path, policy: SecurityPolicy) -> LibraryProvider:
    """Resolve *path* through *policy* and return a reader for its format.

    Requires an explicit :class:`~msmcp.security.SecurityPolicy` so a library
    can never be opened without crossing the boundary.

    Raises
    ------
    UnsupportedFormatError
        If no provider reads *path*'s format.
    """
    resolved = resolve_library_path(path, policy)
    provider_class = _provider_class_for(resolved.name)
    if provider_class is None:  # pragma: no cover - guarded by resolve above
        raise UnsupportedFormatError(f"No library reader for '{resolved.name}'.")
    return provider_class(resolved)
