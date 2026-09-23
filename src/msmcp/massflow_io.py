"""MassFlow-backed ingestion for imaging MS data (imzML).

Why this module exists
----------------------
MassFlow is MSMCP's canonical MS I/O layer, but the installed MassFlow
(0.1.x) is an *imaging* framework: it reads imzML, zarr and HDF5 stores and
exposes no mzML or MGF reader.  MSMCP therefore routes exactly what MassFlow
actually supports through MassFlow, and owns small readers of its own for the
formats MassFlow does not cover (:mod:`msmcp.mzml`, :mod:`msmcp.mgf`).  No
MassFlow API is invented here: only
:class:`massflow.data_manager.MSDataManagerImzML` and the lazy spectrum objects
it produces are used, and their output is converted into MSMCP's own
:class:`~msmcp.mzml.Spectrum`.

The stdout boundary
-------------------
Importing MassFlow's data manager has two process-wide side effects that would
break an MCP server:

1. ``massflow.tools.logger`` initialises itself on import, **removing every
   handler from the root logger** and installing a ``StreamHandler`` writing to
   ``sys.stdout``.  On the stdio transport, any library log line that reaches
   stdout corrupts the JSON-RPC framing and desynchronises the host.
2. It creates a ``logs/`` directory in the process working directory.

:func:`_restore_stderr_logging` runs immediately after the import and repoints
MassFlow's stdout handlers at stderr, so (1) is neutralised for the whole
process.  (2) is MassFlow's own behaviour and cannot be suppressed from here;
it is documented in the README rather than hidden.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, Final

import numpy as np

from msmcp.errors import (
    InaccessiblePathError,
    MalformedFileError,
    MissingDependencyError,
    UnsupportedFormatError,
)
from msmcp.mzml import Spectrum
from msmcp.security import (
    DEFAULT_POLICY,
    SecurityPolicy,
    resolve_path,
    validate_file_size,
)

logger = logging.getLogger("msmcp.massflow_io")

__all__ = [
    "BACKEND",
    "FORMAT",
    "SUFFIXES",
    "declared_spectrum_count",
    "iter_spectra",
    "massflow_version",
    "resolve_imzml_path",
]

FORMAT: Final[str] = "imzML"
"""The format name reported in provenance for files read here."""

BACKEND: Final[str] = "massflow.imzML"
"""Identifier of the reader that actually parsed the file."""

SUFFIXES: Final[tuple[str, ...]] = (".imzml",)
"""File suffixes routed to MassFlow."""


def massflow_version() -> str | None:
    """Return the installed MassFlow version, or ``None`` when absent."""
    import importlib.metadata

    try:
        return importlib.metadata.version("massflow")
    except importlib.metadata.PackageNotFoundError:
        return None


def _restore_stderr_logging() -> None:
    """Point MassFlow's stdout log handlers at stderr.

    ``logging.StreamHandler`` keeps the stream object it was constructed with,
    so replacing ``sys.stdout`` is not enough - the handler must be retargeted.
    """
    stdout_streams = {stream for stream in (sys.stdout, sys.__stdout__) if stream}
    candidates: list[logging.Logger] = [logging.getLogger()]
    for name in list(logging.Logger.manager.loggerDict):
        if name == "massflow" or name.startswith("massflow."):
            candidate = logging.getLogger(name)
            if isinstance(candidate, logging.Logger):
                candidates.append(candidate)

    for candidate in candidates:
        for handler in list(candidate.handlers):
            if isinstance(handler, logging.StreamHandler) and (
                handler.stream in stdout_streams
            ):
                handler.setStream(sys.stderr)
                logger.debug(
                    "Repointed a MassFlow stdout log handler on logger %r to stderr",
                    candidate.name,
                )


def _import_manager() -> Any:
    """Import MassFlow's imzML data manager, keeping stdout clean."""
    try:
        from massflow.data_manager import MSDataManagerImzML
    except ImportError as exc:  # pragma: no cover - massflow is a hard dependency
        raise MissingDependencyError(
            "Reading imzML files requires the MassFlow package.  Install it "
            "with `uv sync` (it is a required MSMCP dependency)."
        ) from exc
    _restore_stderr_logging()
    return MSDataManagerImzML


# ---------------------------------------------------------------------------
# Path validation
# ---------------------------------------------------------------------------
def resolve_imzml_path(
    path: str | Path,
    policy: SecurityPolicy = DEFAULT_POLICY,
) -> Path:
    """Validate an imzML path and its required ``.ibd`` sibling.

    The filesystem boundary is applied to *both* files, so an allowed-root
    check cannot be bypassed by pointing the binary payload elsewhere.
    """
    resolved = resolve_path(path, policy)  # PathEscapeError
    if resolved.name.lower().endswith(".ibd"):
        raise UnsupportedFormatError(
            "'.ibd' is the binary payload of an imzML acquisition, not a "
            "standalone file.  Pass the .imzML header instead."
        )
    if not resolved.name.lower().endswith(".imzml"):
        raise UnsupportedFormatError(
            f"'{resolved.suffix}' is not an imzML file.  Expected a .imzML "
            f"header paired with an .ibd binary payload."
        )

    _check_readable(resolved)
    validate_file_size(resolved, policy)

    ibd = resolved.with_suffix(".ibd")
    if not ibd.is_file():
        raise InaccessiblePathError(
            f"imzML file '{resolved}' is missing its paired binary payload "
            f"'{ibd}'.  Both files are required; copy them together."
        )
    resolve_path(ibd, policy)  # the payload must be inside the root too
    validate_file_size(ibd, policy)
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


# ---------------------------------------------------------------------------
# Conversion
# ---------------------------------------------------------------------------
def _as_coordinate(raw: Any) -> tuple[int, int, int] | None:
    """Convert MassFlow's pixel coordinate object into a plain ``(x, y, z)``.

    MassFlow returns a :class:`~massflow.module.pixel_coordinates.PixelCoordinates`
    value object rather than a tuple, and its shape is not part of MassFlow's
    documented stable API, so each accessor is tried defensively; a coordinate
    that cannot be read is reported as absent rather than guessed at.
    """
    getter: Callable[[], Any] | None = getattr(raw, "get_tuple", None)
    if getter is not None:
        try:
            values = tuple(int(value) for value in getter())
        except Exception:  # pragma: no cover - defensive against MassFlow changes
            return None
        if len(values) == 3:
            return (values[0], values[1], values[2])
        return None

    try:  # pragma: no cover - fallback for a plain sequence
        return (int(raw[0]), int(raw[1]), int(raw[2]))
    except Exception:
        return None


def _to_spectrum(massflow_spectrum: Any, index: int, ms_level: int | None) -> Spectrum:
    """Convert one MassFlow spectrum into MSMCP's canonical representation."""
    massflow_spectrum.resolve_data()
    mz = np.asarray(massflow_spectrum.mz_list, dtype=np.float64)
    intensity = np.asarray(massflow_spectrum.intensity, dtype=np.float64)

    if mz.ndim != 1 or intensity.ndim != 1:
        raise MalformedFileError(
            f"MassFlow returned non-1-D arrays for imzML spectrum {index} "
            f"(m/z shape {mz.shape}, intensity shape {intensity.shape})."
        )
    if mz.size != intensity.size:
        raise MalformedFileError(
            f"MassFlow returned {mz.size} m/z values but {intensity.size} "
            f"intensities for imzML spectrum {index}; the arrays must be "
            f"parallel."
        )

    mz = mz.copy()
    intensity = intensity.copy()
    mz.setflags(write=False)
    intensity.setflags(write=False)

    coordinate: tuple[int, int, int] | None = None
    raw_coordinate = massflow_spectrum.get_coordinates()
    if raw_coordinate is not None:
        coordinate = _as_coordinate(raw_coordinate)

    return Spectrum(
        index=index,
        ms_level=ms_level,
        retention_time=None,
        precursor_mz=None,
        mz=mz,
        intensity=intensity,
        coordinate=coordinate,
    )


def _load(path: Path) -> Any:
    """Load the imzML acquisition into a MassFlow spectrum set."""
    manager_class = _import_manager()
    try:
        manager = manager_class(filepath=str(path))
        manager.load_head_data()
    except Exception as exc:
        raise MalformedFileError(
            f"MassFlow could not read '{path}' as imzML: {type(exc).__name__}: {exc}"
        ) from exc
    return manager


def _acquired_ms_level(spectra: Any) -> int | None:
    """Derive the acquisition's MS level from MassFlow's own metadata.

    Returns ``None`` when MassFlow does not state it, rather than assuming
    MS1 - an imaging acquisition is not necessarily single-level.
    """
    meta = getattr(spectra, "meta", None)
    if meta is None:
        return None
    if getattr(meta, "ms1_spectrum", None) is True:
        return 1
    if getattr(meta, "msn_spectrum", None) is True:
        return None
    return None


def iter_spectra(path: str | Path) -> Iterator[Spectrum]:
    """Stream spectra from an imzML acquisition, one lazy pixel at a time.

    MassFlow materialises one lightweight placeholder per pixel on load and
    resolves each spectrum's arrays on demand, so memory scales with the
    number of *resolved* spectra rather than with the whole image.

    An imaging run may legitimately contain pixels with no peaks, so an
    individual empty spectrum is passed through as-is.  A run in which *no*
    pixel has any peak, however, means the binary payload could not be read:
    that is raised as :class:`~msmcp.errors.MalformedFileError` rather than
    reported as an acquisition of empty spectra.
    """
    resolved = Path(path)
    manager = _load(resolved)
    spectra = manager.ms
    ms_level = _acquired_ms_level(spectra)

    yielded = 0
    any_peaks = False
    try:
        for index, massflow_spectrum in enumerate(spectra):
            spectrum = _to_spectrum(massflow_spectrum, index, ms_level)
            yielded += 1
            any_peaks = any_peaks or spectrum.n_peaks > 0
            yield spectrum
        if yielded and not any_peaks:
            raise MalformedFileError(
                f"MassFlow read {yielded} spectra from '{resolved}' but every "
                f"one is empty.  An acquisition with no peaks anywhere means "
                f"the paired .ibd payload could not be read (truncated or "
                f"mismatched), not that the data is empty."
            )
    finally:
        close = getattr(manager, "close", None)
        if callable(close):
            close()


def declared_spectrum_count(path: str | Path) -> int:
    """Return the number of pixels in the acquisition.

    imzML declares no total in its header that MassFlow exposes cheaply, so
    this reflects the spectra MassFlow materialised for the file.
    """
    manager = _load(Path(path))
    try:
        return len(manager.ms)
    finally:
        close = getattr(manager, "close", None)
        if callable(close):
            close()
