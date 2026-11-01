"""Real spectral-library readers, confined by the acquisition security boundary.

``search_library`` used to treat its ``database_file`` as a seed for a
synthetic in-memory library, so a path was never resolved and nothing validated
it.  This module is the missing other half: it reads a spectral library that the
user already has on disk, in a format they already have it in.

The readers include **MSP / NIST-style text**, the interchange format most
commercial and public libraries ship as; MGF; and a read-only SQLite peak store.
The text readers are deliberately strict: a peak line that does not parse raises
:class:`~msmcp.errors.MalformedFileError` rather than being skipped, and a
``Num Peaks`` header that disagrees with the peak lines read is an error, because
silently dropping or inventing peaks would change the science.

Every path crosses the same :class:`~msmcp.security.SecurityPolicy` as
acquisitions — allowed root (symlinks resolved), file-size limit — and is opened
read-only. Text readers use mode ``"rb"``; the SQLite reader uses a read-only,
immutable URI and rejects SQLite sidecars. The reader never needs, or takes,
write access to a library file. That is the security criterion the audit
attaches to this item: the moment a provider opens ``database_file``, an
unvalidated path would be arbitrary file read from an MCP tool.

The provider shape mirrors :class:`~msmcp.models.embeddings.SpectralEmbedder`
and :class:`~msmcp.execution.executor.JobExecutor` — the third use of the same
pattern — so the scan is written against :class:`LibraryProvider` and additional
readers can be added without touching the search pipeline.
"""

from __future__ import annotations

import gzip
import io
import json
import re
import sqlite3
from abc import ABC, abstractmethod
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import IO, Final, cast

from msmcp import security
from msmcp.errors import (
    MalformedFileError,
    UnsupportedFormatError,
)
from msmcp.mgf import (
    declared_spectrum_count as mgf_declared_spectrum_count,
)
from msmcp.mgf import (
    iter_spectra as iter_mgf_spectra,
)
from msmcp.provenance import file_digest
from msmcp.security import SecurityPolicy, resolve_path, validate_file_size

__all__ = [
    "MGF_SUFFIXES",
    "MSP_SUFFIXES",
    "SQLITE_SUFFIXES",
    "LibraryInfo",
    "LibraryProvider",
    "LibrarySpectrum",
    "MGFLibraryProvider",
    "MSPLibraryProvider",
    "SQLiteLibraryProvider",
    "get_library_provider",
    "is_supported_library_path",
    "resolve_library_path",
]

MGF_SUFFIXES: Final[tuple[str, ...]] = (".mgf", ".mgf.gz")
"""File suffixes read as MGF (Mascot Generic Format) libraries."""

MSP_SUFFIXES: Final[tuple[str, ...]] = (".msp", ".msp.gz")
"""File suffixes read as MSP / NIST-style text libraries."""

SQLITE_SUFFIXES: Final[tuple[str, ...]] = (".sqlite", ".db")
"""File suffixes read as SQLite peak store libraries."""

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
_ION_MODE_KEYS: Final[frozenset[str]] = frozenset(
    {
        "IONMODE",
        "IONIZATIONMODE",
        "POLARITY",
    }
)


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


def _polarity_from_ion_mode(raw: str | None) -> str | None:
    """Map an MSP ``Ion_mode`` value to ``"positive"/"negative"``, else None.

    Libraries spell the field ``Ion_mode: P``/``N`` (GNPS/NIST) or
    ``Positive``/``Negative``; anything unrecognised is left ``None`` so the
    search never filters or flags on a polarity the file did not state.
    """
    if raw is None:
        return None
    token = raw.strip().upper()
    if token in ("P", "POS", "POSITIVE", "+"):
        return "positive"
    if token in ("N", "NEG", "NEGATIVE", "-"):
        return "negative"
    return None


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class LibrarySpectrum:
    """One library entry: a compound name plus a peak list.

    ``peaks`` is a tuple of ``(m/z, intensity)`` float pairs — the exact value
    the scorer consumes — so a library spectrum never needs a second
    representation.  ``polarity`` models the ``Ion_mode`` field the search
    reports on (``"positive"``/``"negative"``, or ``None`` when the record does
    not state one).  Metadata MSMCP still does not model (collision energy,
    InChIKey, ...) is preserved verbatim in :attr:`metadata` rather than dropped,
    so a later reader can use it without a re-parse.
    """

    index: int
    compound_name: str
    precursor_mz: float | None
    formula: str | None
    peaks: tuple[tuple[float, float], ...]
    polarity: str | None = None
    metadata: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class LibraryInfo:
    """What a provider can say about a library without returning its spectra.

    ``n_spectra`` is the number of spectra that will actually be searched, and
    ``n_records_without_peaks`` counts the records the reader skipped because
    they hold no peaks at all.  Both are reported: a reader that drops records
    without saying how many is as untrustworthy as one that refuses to read the
    file.
    """

    path: str
    name: str
    format: str
    version: str | None
    n_spectra: int
    digest: str | None
    size_bytes: int | None
    n_records_without_peaks: int = 0


@dataclass(slots=True)
class MSPParseStats:
    """Counters the MSP reader fills in while it streams a library."""

    spectra: int = 0
    records_without_peaks: int = 0


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

    def validate(self) -> None:
        """Prove the library opens and parses, using a bounded prefix.

        Reads the first record through the same parser the scan uses, so a file
        that is not the format its suffix claims — or whose first record is
        corrupt — fails **before** a search is dispatched, exactly as a
        malformed query spectrum does on the query path.  An empty library is
        legal and passes: it is searched, finds nothing, and says so.

        Only the first record is read, so corruption that begins further into
        the file cannot be caught here without reading all of it; that case
        surfaces as a failed job carrying the parser's traceback, which the
        poller reports.
        """
        for _chunk in self.iter_spectra(1):
            return


# ---------------------------------------------------------------------------
# MSP / NIST-style text
# ---------------------------------------------------------------------------
def _open_text(path: Path) -> IO[str]:
    """Open *path* read-only as text, transparently handling gzip.

    A gzip payload is bounded by its *decompressed* size, not only by the bytes
    on disk (see :func:`msmcp.security.bounded_stream`).
    """
    handle = path.open("rb")
    magic = handle.read(2)
    handle.seek(0)
    if magic == b"\x1f\x8b":
        buffered = gzip.GzipFile(fileobj=handle)
        bounded = security.bounded_stream(
            buffered, security.DEFAULT_POLICY.max_file_size_bytes
        )
        return io.TextIOWrapper(cast(IO[bytes], bounded))
    return io.TextIOWrapper(handle)


def _parse_peak_line(line: str, line_number: int) -> list[tuple[float, float]]:
    """Parse one MSP peak line into one or more ``(m/z, intensity)`` pairs.

    Three spellings occur in libraries in the wild, and all three are read:

    ``110.0713 40.0``
        one whitespace-separated pair per line — the ordinary form;
    ``110.0713 40.0; 120.0808 100.0``
        several ``;``-separated pairs on one line;
    ``110.0713:40.0 120.0808:100.0``
        several ``m/z:intensity`` pairs separated by whitespace — the GMD/Golm
        GC-MS exports write this, and they are shipped in the Fiehn/Golm
        libraries as ``GMD_20111121_*_MSP.msp``.

    A group that mixes the spellings is not guessed at: the colon form is
    recognised only when *every* field carries a colon, and the whitespace form
    only when there are exactly two fields.  Anything else raises, so a line is
    never reinterpreted as something shorter than what it actually says.  An
    optional trailing annotation in double quotes is ignored, and a group that
    does not yield two numeric fields is a malformed line, not a peak to skip.
    """
    cleaned = _QUOTED_RE.sub(" ", line)
    pairs: list[tuple[float, float]] = []
    for group in cleaned.split(";"):
        if not group.strip():
            continue
        parts = group.split()
        colon_fields = sum(1 for part in parts if ":" in part)
        if colon_fields:
            if colon_fields != len(parts):
                raise MalformedFileError(
                    f"MSP peak line {line_number} mixes the '<m/z> <intensity>' and "
                    f"'<m/z>:<intensity>' spellings: {line!r}"
                )
            for part in parts:
                mz_raw, _, intensity_raw = part.partition(":")
                pairs.append(_pair(mz_raw, intensity_raw, line, line_number))
            continue
        # The whitespace form carries exactly one pair per group.  A longer
        # group is not read as the first two fields and the rest discarded:
        # dropping peaks without saying so is the failure this reader exists to
        # avoid, and the strict `Num Peaks` check only catches it by accident.
        if len(parts) != 2:
            raise MalformedFileError(
                f"MSP peak line {line_number} is not '<m/z> <intensity>': {line!r}"
            )
        pairs.append(_pair(parts[0], parts[1], line, line_number))
    return pairs


def _pair(
    mz_raw: str, intensity_raw: str, line: str, line_number: int
) -> tuple[float, float]:
    """Parse one ``m/z`` / ``intensity`` field pair, refusing non-numeric input."""
    try:
        return float(mz_raw), float(intensity_raw)
    except ValueError as exc:
        raise MalformedFileError(
            f"MSP peak line {line_number} has a non-numeric value: {line!r}"
        ) from exc


def _build_spectrum(
    index: int,
    line_number: int,
    fields: dict[str, str],
    peaks: list[tuple[float, float]],
) -> LibrarySpectrum | None:
    """Turn one accumulated MSP block into a :class:`LibrarySpectrum`.

    Returns ``None`` for a record that holds no peaks — a metadata-only
    deposition, or one whose ``Num Peaks: 0`` says outright that it has none.
    These are ordinary in the public GNPS/MoNA libraries: ``GNPS-LIBRARY.msp``
    has one at record 1,242, ``GNPS-NIST14-MATCHES.msp`` at 20 and
    ``DEREPLICATOR_IDENTIFIED_LIBRARY.msp`` at 57.  An entry with a structure and
    no spectrum has nothing to match against, so it is skipped — and counted by
    the caller, because a reader that drops records silently is no better than
    one that fails outright.

    The declaration is checked *before* that decision either way, so the
    strictness that earns its keep is untouched: a ``Num Peaks`` header that
    disagrees with the peak lines read is still a
    :class:`~msmcp.errors.MalformedFileError`, including the case where it
    declares peaks and none follow.
    """
    name = _first(fields, _NAME_KEYS) or ""

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

    if not peaks:
        return None

    return LibrarySpectrum(
        index=index,
        compound_name=name,
        precursor_mz=_to_float_or_none(_first(fields, _PRECURSOR_KEYS)),
        formula=_first(fields, _FORMULA_KEYS),
        peaks=tuple(peaks),
        polarity=_polarity_from_ion_mode(_first(fields, _ION_MODE_KEYS)),
        metadata=MappingProxyType(dict(fields)),
    )


def _first(fields: dict[str, str], keys: frozenset[str]) -> str | None:
    """Return the first value whose normalised key is in *keys*."""
    for key in keys:
        if key in fields:
            return fields[key]
    return None


def iter_msp_spectra(
    path: Path, stats: MSPParseStats | None = None
) -> Iterator[LibrarySpectrum]:
    """Stream :class:`LibrarySpectrum` objects from an MSP file.

    Records are separated by a blank line or by the next ``Name:`` line.  The
    reader raises on any malformed peak line or mismatched ``Num Peaks`` header,
    so a corrupt record is never silently repaired; a record that holds no peaks
    at all is skipped, and counted in *stats* when one is supplied, rather than
    invented or treated as fatal.
    """
    handle = _open_text(path)
    fields: dict[str, str] = {}
    peaks: list[tuple[float, float]] = []
    have_content = False
    index = 0
    line_number = 0

    def _emit() -> Iterator[LibrarySpectrum]:
        """Build the accumulated record, yielding it only if it holds peaks."""
        spectrum = _build_spectrum(index, line_number, fields, peaks)
        if spectrum is None:
            if stats is not None:
                stats.records_without_peaks += 1
            return
        if stats is not None:
            stats.spectra += 1
        yield spectrum

    try:
        for raw_line in handle:
            line_number += 1
            line = raw_line.strip()
            if not line:
                if have_content:
                    yield from _emit()
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
                yield from _emit()
                index += 1
                fields, peaks = {}, []
            fields[key] = match.group(2).strip()
            have_content = True

        if have_content:
            yield from _emit()
    finally:
        handle.close()


class MSPLibraryProvider(LibraryProvider):
    """Read a spectral library stored as MSP / NIST-style text."""

    format = "MSP"

    def describe(self) -> LibraryInfo:
        """Count the library's spectra and digest the file it came from.

        The count is taken with the same reader the scan uses, so the number
        reported is the number that will actually be searched — and the records
        the reader skipped for holding no peaks are reported beside it rather
        than left unmentioned.
        """
        stats = MSPParseStats()
        for _spectrum in iter_msp_spectra(self._path, stats):
            pass
        try:
            size = self._path.stat().st_size
        except OSError:
            size = None
        return LibraryInfo(
            path=str(self._path),
            name=self._path.name,
            format=self.format,
            version="NIST/MSP text",
            n_spectra=stats.spectra,
            digest=file_digest(self._path),
            size_bytes=size,
            n_records_without_peaks=stats.records_without_peaks,
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


class MGFLibraryProvider(LibraryProvider):
    """Read a spectral library stored as MGF (Mascot Generic Format)."""

    format = "MGF"

    def describe(self) -> LibraryInfo:
        """Count the library's spectra and digest the file it came from."""
        try:
            size = self._path.stat().st_size
        except OSError:
            size = None
        return LibraryInfo(
            path=str(self._path),
            name=self._path.name,
            format=self.format,
            version="MGF",
            n_spectra=mgf_declared_spectrum_count(self._path),
            digest=file_digest(self._path),
            size_bytes=size,
            n_records_without_peaks=0,
        )

    def iter_spectra(self, chunk_size: int = 2000) -> Iterator[list[LibrarySpectrum]]:
        """Yield the library's spectra in chunks of at most *chunk_size*."""
        if chunk_size < 1:
            raise ValueError(f"chunk_size must be positive, got {chunk_size!r}")

        chunk: list[LibrarySpectrum] = []
        for spectrum in iter_mgf_spectra(self._path):
            peaks = list(
                zip(spectrum.mz.tolist(), spectrum.intensity.tolist(), strict=True)
            )

            metadata: dict[str, str] = {}
            if spectrum.ms_level is not None:
                metadata["MSLEVEL"] = str(spectrum.ms_level)
            if spectrum.retention_time is not None:
                metadata["RTINMINUTES"] = str(spectrum.retention_time)
            if spectrum.charge is not None:
                metadata["CHARGE"] = str(spectrum.charge)
            if spectrum.isolation_window is not None:
                metadata["ISOLATION_WINDOW"] = str(spectrum.isolation_window)

            lib_spectrum = LibrarySpectrum(
                index=spectrum.index or 0,
                compound_name=f"Spectrum {spectrum.index or 0}",
                precursor_mz=spectrum.precursor_mz,
                formula=None,
                peaks=tuple(peaks),
                polarity=spectrum.polarity,
                metadata=MappingProxyType(metadata),
            )
            chunk.append(lib_spectrum)
            if len(chunk) >= chunk_size:
                yield chunk
                chunk = []
        if chunk:
            yield chunk


class SQLiteLibraryProvider(LibraryProvider):
    """Read a spectral library stored as a local SQLite peak store.

    The SQLite schema consists of two tables:
    - ``spectra``: ``id`` (INTEGER PRIMARY KEY), ``compound_name`` (TEXT),
      ``precursor_mz`` (REAL), ``formula`` (TEXT), ``polarity`` (INTEGER),
      ``metadata`` (JSON).  Polarity is +1 for positive, -1 for negative,
      and NULL for unknown.
    - ``peaks``: ``spectrum_id`` (INTEGER), ``mz`` (REAL), ``intensity`` (REAL),
      FOREIGN KEY(``spectrum_id``) REFERENCES ``spectra``(``id``).
    """

    format = "SQLite"

    _SIDECAR_SUFFIXES: Final[tuple[str, ...]] = ("-wal", "-shm", "-journal")

    def _ensure_sidecar_free(self) -> None:
        """Require a checkpointed, sidecar-free database for stable provenance.

        SQLite may read committed rows from a WAL or recover a rollback journal
        while opening a database. Those bytes are outside the validated file
        size and the main-file digest, so accepting them would break both
        boundaries. Reject even empty sidecars and open the main file as
        immutable so SQLite will not create or consult sidecars.
        """
        for suffix in self._SIDECAR_SUFFIXES:
            sidecar = Path(f"{self._path}{suffix}")
            try:
                sidecar.lstat()
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise MalformedFileError(
                    f"Cannot inspect SQLite sidecar for '{self._path}': {exc}"
                ) from exc
            raise MalformedFileError(
                "SQLite libraries must be checkpointed and sidecar-free; "
                f"found '{sidecar.name}'."
            )

    @staticmethod
    def _validate_schema(conn: sqlite3.Connection) -> None:
        """Validate required tables and columns, including for empty stores."""
        conn.execute(
            "SELECT id, compound_name, precursor_mz, formula, polarity, metadata "
            "FROM spectra LIMIT 0"
        )
        conn.execute("SELECT spectrum_id, mz, intensity FROM peaks LIMIT 0")

    def _connect(self) -> sqlite3.Connection:
        self._ensure_sidecar_free()
        # as_uri percent-encodes ?, #, %, and other URI path delimiters.
        # immutable=1 prevents SQLite from consulting or creating sidecars.
        uri = f"{self._path.as_uri()}?mode=ro&immutable=1"
        conn = sqlite3.connect(uri, uri=True)
        conn.enable_load_extension(False)

        def authorizer(action, arg1, arg2, dbname, source):
            if action == sqlite3.SQLITE_ATTACH:
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        conn.set_authorizer(authorizer)

        return conn

    def describe(self) -> LibraryInfo:
        """Count the library's spectra and digest the file it came from."""
        try:
            size = self._path.stat().st_size
        except OSError:
            size = None

        n_spectra = 0
        n_records_without_peaks = 0
        try:
            with self._connect() as conn:
                self._validate_schema(conn)
                row = conn.execute(
                    "SELECT COUNT(*), "
                    "SUM(CASE WHEN EXISTS ("
                    "SELECT 1 FROM peaks WHERE peaks.spectrum_id = spectra.id"
                    ") THEN 1 ELSE 0 END) FROM spectra"
                ).fetchone()
                if row is not None:
                    total, with_peaks = row
                    n_spectra = with_peaks or 0
                    n_records_without_peaks = total - n_spectra
        except sqlite3.Error as e:
            raise MalformedFileError(f"Failed to read SQLite library: {e}") from e

        return LibraryInfo(
            path=str(self._path),
            name=self._path.name,
            format=self.format,
            version="SQLite peak store",
            n_spectra=n_spectra,
            digest=file_digest(self._path),
            size_bytes=size,
            n_records_without_peaks=n_records_without_peaks,
        )

    def iter_spectra(self, chunk_size: int = 2000) -> Iterator[list[LibrarySpectrum]]:
        """Yield the library's spectra in chunks of at most *chunk_size*."""
        if chunk_size < 1:
            raise ValueError(f"chunk_size must be positive, got {chunk_size!r}")

        try:
            with self._connect() as conn:
                self._validate_schema(conn)
                cursor = conn.cursor()

                variable_limit = conn.getlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER)
                if variable_limit < 1:
                    raise MalformedFileError(
                        "SQLite reports an invalid SQL variable limit."
                    )

                # Records without peaks are deliberately excluded, matching
                # the MSP provider's search and FDR semantics.
                cursor.execute(
                    "SELECT id, compound_name, precursor_mz, formula, polarity, metadata "
                    "FROM spectra WHERE EXISTS ("
                    "SELECT 1 FROM peaks WHERE peaks.spectrum_id = spectra.id"
                    ") ORDER BY id"
                )

                chunk: list[LibrarySpectrum] = []
                while True:
                    fetch_size = min(chunk_size - len(chunk), variable_limit)
                    spectrum_rows = cursor.fetchmany(fetch_size)
                    if not spectrum_rows:
                        break

                    # Fetch peaks for this batch
                    spectrum_ids = [row[0] for row in spectrum_rows]
                    placeholders = ",".join("?" for _ in spectrum_ids)

                    peaks_cursor = conn.cursor()
                    peaks_cursor.execute(
                        f"SELECT spectrum_id, mz, intensity FROM peaks "
                        f"WHERE spectrum_id IN ({placeholders}) "
                        "ORDER BY spectrum_id, mz",
                        spectrum_ids,
                    )

                    peaks_by_id: dict[int, list[tuple[float, float]]] = {
                        sid: [] for sid in spectrum_ids
                    }
                    for peak_row in peaks_cursor.fetchall():
                        peaks_by_id[peak_row[0]].append((peak_row[1], peak_row[2]))

                    for row in spectrum_rows:
                        sid, name, precursor, formula, polarity_int, metadata_json = row

                        polarity_str = None
                        if polarity_int == 1:
                            polarity_str = "positive"
                        elif polarity_int == -1:
                            polarity_str = "negative"

                        metadata: dict[str, str] = {}
                        if metadata_json not in (None, ""):
                            try:
                                decoded = json.loads(metadata_json)
                            except (TypeError, ValueError) as exc:
                                raise MalformedFileError(
                                    f"Invalid JSON metadata for SQLite spectrum {sid}."
                                ) from exc
                            if not isinstance(decoded, dict) or any(
                                not isinstance(key, str) or not isinstance(value, str)
                                for key, value in decoded.items()
                            ):
                                raise MalformedFileError(
                                    "SQLite metadata must be a JSON object with "
                                    f"string keys and values (spectrum {sid})."
                                )
                            metadata = cast(dict[str, str], decoded)

                        lib_spectrum = LibrarySpectrum(
                            index=sid,
                            compound_name=name or f"Spectrum {sid}",
                            precursor_mz=precursor,
                            formula=formula,
                            peaks=tuple(peaks_by_id[sid]),
                            polarity=polarity_str,
                            metadata=MappingProxyType(metadata),
                        )
                        chunk.append(lib_spectrum)

                        if len(chunk) >= chunk_size:
                            yield chunk
                            chunk = []

                if chunk:
                    yield chunk

        except sqlite3.Error as e:
            raise MalformedFileError(f"Failed to read SQLite library: {e}") from e


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
    if lowered.endswith(MGF_SUFFIXES):
        return MGFLibraryProvider
    if lowered.endswith(SQLITE_SUFFIXES):
        return SQLiteLibraryProvider
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
            f"MSMCP reads MSP/NIST-style text, MGF and SQLite ({', '.join(MSP_SUFFIXES + MGF_SUFFIXES + SQLITE_SUFFIXES)}); the "
            f"path was not opened."
        )
    security.validate_readable_file(resolved)  # InaccessiblePathError
    validate_file_size(resolved, policy)  # FileSizeExceededError
    return resolved


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
