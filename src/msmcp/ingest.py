"""Format dispatch for MS ingestion: one entry point, one canonical output.

The architecture is::

    file -> (MassFlow | msmcp reader) -> MSMCP Spectrum -> DataReference -> tools

MassFlow is the canonical layer for everything it supports (imzML, and its
zarr/HDF5 stores); MSMCP owns small readers for the formats MassFlow does not
cover (mzML, MGF).  :func:`resolve_source` picks the reader from the file
extension, records *which* reader it chose, and returns a :class:`Source` whose
spectra are all the same :class:`~msmcp.mzml.Spectrum` type - so no downstream
tool needs to know or care where the data came from.

The chosen reader is carried into provenance as ``backend``, which is what makes
it possible to tell later whether two results were produced by the same parsing
path.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from msmcp import massflow_io, mzml
from msmcp.errors import UnsupportedFormatError
from msmcp.mgf import declared_spectrum_count as _mgf_spectrum_count
from msmcp.mgf import iter_spectra as _iter_mgf_spectra
from msmcp.mgf import resolve_mgf_path
from msmcp.mzml import Spectrum
from msmcp.security import DEFAULT_POLICY, SecurityPolicy

__all__ = ["SUPPORTED_FORMATS", "VENDOR_FORMATS", "Source", "resolve_source"]

SUPPORTED_FORMATS: Final[tuple[str, ...]] = ("mzML", "MGF", "imzML")
"""Formats MSMCP can currently ingest."""

VENDOR_FORMATS: Final[dict[str, str]] = {
    ".raw": "Thermo",
    ".d": "Agilent / Bruker",
    ".wiff": "SCIEX",
    ".mzxml": "mzXML (legacy)",
}
"""Recognised formats MSMCP cannot read, with the vendor that produces them."""

_VENDOR_GUIDANCE: Final[str] = (
    "convert it to .mzML with ProteoWizard MSConvert and retry (imzML imaging "
    "data may be passed through directly as .imzML)"
)


@dataclass(frozen=True, slots=True)
class Source:
    """A validated MS file plus the reader that will parse it."""

    path: Path
    format: str
    backend: str
    """Identifier of the parsing implementation, recorded in provenance."""

    def iter_spectra(self) -> Iterator[Spectrum]:
        """Stream this source's spectra, one at a time."""
        if self.backend == massflow_io.BACKEND:
            return massflow_io.iter_spectra(self.path)
        if self.backend == "msmcp.mgf":
            return _iter_mgf_spectra(self.path)
        return mzml.iter_spectra(self.path)

    def declared_spectrum_count(self) -> int:
        """Return the source's total spectrum count, as cheaply as possible."""
        if self.backend == massflow_io.BACKEND:
            return massflow_io.declared_spectrum_count(self.path)
        if self.backend == "msmcp.mgf":
            return _mgf_spectrum_count(self.path)
        return mzml.declared_spectrum_count(self.path)


def _detect_format(name: str) -> tuple[str, str]:
    """Map a filename to ``(format, backend)``, or explain why it is unsupported."""
    lowered = name.lower()

    if lowered.endswith(massflow_io.SUFFIXES):
        return massflow_io.FORMAT, massflow_io.BACKEND

    if lowered.endswith((".mzml", ".mzml.gz")):
        return "mzML", "msmcp.mzml"

    if lowered.endswith((".mgf", ".mgf.gz")):
        return "MGF", "msmcp.mgf"

    for suffix, vendor in VENDOR_FORMATS.items():
        if lowered.endswith(suffix):
            raise UnsupportedFormatError(
                f"Unsupported vendor format '{suffix}' ({vendor}).  MSMCP "
                f"cannot read it directly; {_VENDOR_GUIDANCE}."
            )

    raise UnsupportedFormatError(
        f"Unrecognised file extension '{Path(lowered).suffix}'. MSMCP reads "
        f"{', '.join(SUPPORTED_FORMATS)}."
    )


def resolve_source(
    path: str | Path,
    policy: SecurityPolicy | None = None,
) -> Source:
    """Validate *path* and select the reader for its format.

    The filesystem boundary (allowed root, file size, readability) is enforced
    by the format-specific resolver, so every ingestion path shares one set of
    limits.  ``policy`` defaults to :data:`msmcp.security.DEFAULT_POLICY`,
    looked up at call time so tests and embedders can substitute it.
    """
    effective = policy if policy is not None else DEFAULT_POLICY
    name = str(path)
    fmt, backend = _detect_format(name)

    if backend == massflow_io.BACKEND:
        resolved = massflow_io.resolve_imzml_path(path, effective)
    elif backend == "msmcp.mgf":
        resolved = resolve_mgf_path(path, effective)
    else:
        resolved = mzml.resolve_mzml_path(path, effective)

    return Source(path=resolved, format=fmt, backend=backend)
