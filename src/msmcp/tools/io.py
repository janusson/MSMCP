"""I/O tools: MS ingestion, server-side data references and compact summaries.

Two kinds of result are produced here, deliberately:

* **compact text** for a human or model to read directly (``load_mzml_summary``,
  ``summarise_reference``);
* a **data reference** for anything large, so a peak table never has to pass
  through MCP/JSON to be usable by the next tool (``load_spectrum``).

The reference flow is the important one::

    load_spectrum(file) -> "ptr:spectrum:<id>"   # a few dozen tokens
    summarise_reference("ptr:spectrum:<id>")     # no re-read of the file
    release_reference("ptr:spectrum:<id>")       # deterministic cleanup

`load_spectrum` registers the parsed spectrum with the server-side store and
returns metadata plus provenance, but never the peaks themselves.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from typing import Annotated, Any

import numpy as np
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from msmcp import ingest
from msmcp.errors import SpectrumIndexError
from msmcp.mzml import declared_spectrum_count, resolve_mzml_path
from msmcp.mzml import iter_spectra as iter_mzml_spectra
from msmcp.provenance import SourceRef, provenance_for
from msmcp.security import DEFAULT_POLICY, validate_spectrum_count
from msmcp.state import store as reference_store
from msmcp.state.pointers import DataReference, StoreLimitError

logger = logging.getLogger("msmcp.tools.io")


# ---------------------------------------------------------------------------
# Pydantic schemas
#
# These models enforce argument constraints inside the tool body.  The
# human-readable parameter descriptions deliberately live on the tool
# *signature* instead: the MCP SDK builds ``inputSchema`` from the signature,
# so only ``Annotated`` fields there are ever shown to the host LLM.
# ---------------------------------------------------------------------------
class MzMLParseInput(BaseModel):
    """Validated input for the load_mzml_summary tool."""

    file_path: str = Field(...)
    max_spectra: int = Field(default=5, ge=1, le=50)
    noise_threshold: float = Field(default=0.0, ge=0.0)


class LoadSpectrumInput(BaseModel):
    """Validated input for the load_spectrum tool."""

    file_path: str = Field(..., min_length=1)
    spectrum_index: int = Field(default=0, ge=0)


class ReferenceInput(BaseModel):
    """Validated input for the reference-addressed tools."""

    reference: str = Field(..., min_length=1)


class SpectrumReferenceResult(BaseModel):
    """Structured result of ``load_spectrum``.

    Describes the registered spectrum in full without containing it: the peaks
    stay on the server behind ``reference``.
    """

    reference: str = Field(description="Opaque server-side data reference.")
    kind: str = Field(description="Data kind held by the reference.")
    format: str = Field(description="Detected source format, e.g. 'mzML'.")
    backend: str = Field(description="Reader that parsed the file.")
    source: str = Field(description="Path the spectrum was read from.")
    spectrum_index: int = Field(description="Index of the spectrum within the source.")
    ms_level: int | None = Field(description="MS level, when the source states it.")
    retention_time: float | None = Field(description="Retention time in minutes.")
    precursor_mz: float | None = Field(description="Precursor m/z, when present.")
    coordinate: list[int] | None = Field(
        description="(x, y, z) pixel for imaging sources; null otherwise."
    )
    n_peaks: int = Field(description="Number of peaks held server-side.")
    mz_range: list[float] | None = Field(description="[min m/z, max m/z], or null.")
    tic: float = Field(description="Total ion current (sum of all intensities).")
    n_bytes: int | None = Field(description="Size of the stored peak arrays.")
    provenance: dict[str, Any] = Field(
        description="Structured provenance for this derived object."
    )


# ---------------------------------------------------------------------------
# Formatting helpers (token-efficient, fixed precision)
# ---------------------------------------------------------------------------
def _fmt_mz(val: float) -> str:
    """m/z values -> 4 decimal places."""
    return f"{val:.4f}"


def _fmt_rt(val: float) -> str:
    """Retention time -> 2 decimal places."""
    return f"{val:.2f}"


def _fmt_intensity(val: float) -> str:
    """Intensity -> scientific notation above 1e6, otherwise 2 decimal places."""
    return f"{val:.2e}" if abs(val) >= 1e6 else f"{val:.2f}"


# ---------------------------------------------------------------------------
# Per-spectrum summary builder
# ---------------------------------------------------------------------------
def _summarise_spectrum(
    spectrum: Any,
    noise_threshold: float,
    *,
    top_n: int = 10,
) -> str:
    """Return a compact text block describing one spectrum."""
    lines: list[str] = []

    idx = _maybe_int(getattr(spectrum, "index", None))
    ms_level = _maybe_int(getattr(spectrum, "ms_level", None))
    rt = getattr(spectrum, "retention_time", None)
    coordinate = getattr(spectrum, "coordinate", None)

    header = f"Spectrum #{idx}  |  MS{ms_level}  |  RT: {_safe_fmt(rt, _fmt_rt)} min"
    if coordinate is not None:
        header += f"  |  pixel {tuple(coordinate)}"
    lines.append(header)

    mz = _to_float64(getattr(spectrum, "mz", None))
    intensity = _to_float64(getattr(spectrum, "intensity", None))

    if mz is None or intensity is None or len(mz) == 0:
        lines.append("  (no peak data)")
        return "\n".join(lines)

    # Total ion current is the exact sum of *all* intensities, computed before
    # any noise thresholding so the number remains truthful.
    tic = float(np.sum(intensity, dtype=np.float64))
    lines.append(f"  TIC: {_fmt_intensity(tic)}")

    if noise_threshold > 0.0:
        keep = intensity >= noise_threshold
        mz = mz[keep]
        intensity = intensity[keep]

    n_peaks = len(mz)
    lines.append(f"  Peaks (>={noise_threshold:.1f}): {n_peaks}")

    if n_peaks == 0:
        lines.append("  (all peaks below noise threshold)")
        return "\n".join(lines)

    order = np.argsort(intensity)[::-1]  # descending
    show = min(top_n, n_peaks)
    lines.append(f"  Top {show} peaks (m/z -> intensity):")
    for i in range(show):
        peak = int(order[i])
        lines.append(f"    {_fmt_mz(mz[peak])} -> {_fmt_intensity(intensity[peak])}")

    base_peak = int(np.argmax(intensity))
    lines.append(
        f"  Base peak: {_fmt_mz(mz[base_peak])}  "
        f"({_fmt_intensity(intensity[base_peak])})"
    )
    lines.append("")  # blank separator between spectra
    return "\n".join(lines)


def _provenance_block(provenance: dict[str, Any] | None) -> str:
    """Render provenance as a compact, model-readable footer."""
    if not provenance:
        return ""
    software = provenance.get("software", {}).get("versions", {})
    versions = ", ".join(f"{name} {version}" for name, version in software.items())
    sources = provenance.get("sources") or []
    lines = [
        "",
        "-" * 50,
        f"Provenance: {provenance.get('operation')} at {provenance.get('created_at')}",
    ]
    for source in sources:
        lines.append(
            f"  source: {source.get('path')} "
            f"(format={source.get('format')}, reader={source.get('backend')}, "
            f"digest={source.get('digest')})"
        )
    if provenance.get("parents"):
        lines.append(f"  parent references: {', '.join(provenance['parents'])}")
    if provenance.get("model"):
        lines.append(f"  model: {provenance['model']}")
    if versions:
        lines.append(f"  software: {versions}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------
def _to_float64(arr: Any) -> np.ndarray | None:
    """Coerce to float64, returning None for missing or non-1-D input."""
    if arr is None:
        return None
    try:
        out = np.asarray(arr, dtype=np.float64)
        return out if out.ndim == 1 else None
    except (ValueError, TypeError):
        return None


def _maybe_int(val: Any) -> int | str:
    """Return the integer value, or '?' when it is missing or non-numeric."""
    if val is None:
        return "?"
    try:
        return int(val)
    except (TypeError, ValueError):
        return "?"


def _safe_fmt(val: Any, fmt_fn: Callable[[float], str]) -> str:
    """Apply *fmt_fn* to *val*, returning 'N/A' for None."""
    return "N/A" if val is None else fmt_fn(float(val))


# ===================================================================
# Public tools
# ===================================================================
def register_tools(mcp: Any) -> None:
    """Register I/O tools on the supplied MCPServer *mcp* instance."""

    @mcp.tool(
        title="Summarise an mzML file",
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False),
    )
    def load_mzml_summary(
        file_path: Annotated[
            str,
            Field(
                description="Path to a local .mzML file, absolute or relative "
                "to the server working directory."
            ),
        ],
        max_spectra: Annotated[
            int,
            Field(
                ge=1,
                le=50,
                description="How many spectra to summarise. Raise it to look "
                "further into the run.",
            ),
        ] = 5,
        noise_threshold: Annotated[
            float,
            Field(
                ge=0.0,
                description="Minimum absolute intensity; peaks below this are "
                "omitted. 0.0 keeps every peak.",
            ),
        ] = 0.0,
    ) -> str:
        """Summarise the first spectra of a local .mzML file as compact text.

        For each spectrum reports MS level, retention time, total ion current,
        peak count, the ten most intense peaks and the base peak.  Use it to
        inspect an acquisition before scoring or searching it.

        Output is capped at *max_spectra* spectra to stay inside the context
        window; the footer states the file's total spectrum count.
        """
        _ = MzMLParseInput(
            file_path=file_path,
            max_spectra=max_spectra,
            noise_threshold=noise_threshold,
        )
        validate_spectrum_count(max_spectra, DEFAULT_POLICY)
        resolved_path = resolve_mzml_path(file_path, DEFAULT_POLICY)

        total = declared_spectrum_count(resolved_path)
        header = [
            f"File: {os.path.basename(file_path)}",
            "Format: MZML  (reader: msmcp.mzml)",
            "-" * 50,
        ]

        body: list[str] = []
        count = 0
        for spectrum in iter_mzml_spectra(resolved_path):
            if count >= max_spectra:
                break
            body.append(_summarise_spectrum(spectrum, noise_threshold))
            count += 1

        footer = ["-" * 50, f"Summarised {count} of {total} total spectra."]
        if total > max_spectra:
            footer.append(
                "(Additional spectra omitted - raise `max_spectra` to see more.)"
            )

        logger.info(
            "load_mzml_summary(file=%r, spectra=%d, total=%d, noise=%.2f)",
            file_path,
            count,
            total,
            noise_threshold,
        )
        return "\n".join(header + body + footer)

    @mcp.tool(
        title="Load a spectrum as a server-side reference",
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False),
    )
    def load_spectrum(
        file_path: Annotated[
            str,
            Field(
                min_length=1,
                description="Path to a local .mzML, .mzML.gz, .mgf or .imzML "
                "file. The reader is chosen from the file extension.",
            ),
        ],
        spectrum_index: Annotated[
            int,
            Field(
                ge=0,
                description="Zero-based index of the spectrum to load within the file.",
            ),
        ] = 0,
    ) -> SpectrumReferenceResult:
        """Load one spectrum server-side and return its data reference.

        The peaks stay on the server: this returns a short ``ptr:spectrum:...``
        reference plus metadata and provenance, never the peak arrays.  Pass the
        reference to other tools (for example `compute_cosine` or
        `search_library`) so a large spectrum never has to be copied through the
        conversation.

        Use `summarise_reference` to read the spectrum's contents and
        `release_reference` when you are done with it.  References expire
        automatically after a retention period.
        """
        validated = LoadSpectrumInput(
            file_path=file_path, spectrum_index=spectrum_index
        )
        source = ingest.resolve_source(validated.file_path, DEFAULT_POLICY)

        spectrum = None
        for current, candidate in enumerate(source.iter_spectra()):
            if current == validated.spectrum_index:
                spectrum = candidate
                break
        if spectrum is None:
            raise SpectrumIndexError(
                f"'{validated.file_path}' contains no spectrum at index "
                f"{validated.spectrum_index}.  The file was read successfully, "
                f"so the index is out of range; check the file's spectrum count."
            )

        provenance = provenance_for(
            "load_spectrum",
            parameters={
                "spectrum_index": validated.spectrum_index,
                "source_format": source.format,
                "reader_backend": source.backend,
            },
            sources=(
                SourceRef.from_path(
                    source.path, format=source.format, backend=source.backend
                ),
            ),
        )
        try:
            reference = reference_store.store_spectrum(spectrum, provenance=provenance)
        except StoreLimitError as exc:
            raise StoreLimitError(
                f"{exc}  Release references you no longer need with "
                f"`release_reference` before storing more data."
            ) from exc

        mz = np.asarray(spectrum.mz, dtype=np.float64)
        logger.info(
            "load_spectrum(file=%r, index=%d) -> %s (%d peaks)",
            file_path,
            validated.spectrum_index,
            reference.identifier,
            mz.size,
        )
        return _reference_result(
            reference=reference,
            source_path=str(source.path),
            fmt=source.format,
            backend=source.backend,
            spectrum_index=validated.spectrum_index,
            ms_level=spectrum.ms_level,
            retention_time=spectrum.retention_time,
            precursor_mz=spectrum.precursor_mz,
            coordinate=list(spectrum.coordinate) if spectrum.coordinate else None,
            mz=mz,
            tic=spectrum.tic,
        )

    @mcp.tool(
        title="Summarise a referenced spectrum",
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False),
    )
    def summarise_reference(
        reference: Annotated[
            str,
            Field(
                min_length=1,
                description="A data reference returned by load_spectrum, e.g. "
                "'ptr:spectrum:1a2b...'.",
            ),
        ],
    ) -> str:
        """Summarise a spectrum held behind a data reference.

        Reads the peaks from server memory: the source file is not opened
        again, and nothing large is transferred.  Use it to inspect data you
        have already loaded with `load_spectrum`.

        Reports the same compact per-spectrum block as `load_mzml_summary`,
        followed by the provenance of the referenced object.
        """
        validated = ReferenceInput(reference=reference)
        spectrum = reference_store.resolve_spectrum(validated.reference)
        stored = reference_store.reference_summary(validated.reference)

        tic = float(np.sum(np.asarray(spectrum.intensity, dtype=np.float64)))
        body = _summarise_spectrum(spectrum, 0.0)
        lines = [
            f"Reference: {validated.reference}",
            f"Held server-side: {stored.get('shape')} ({stored.get('n_bytes')} bytes)",
            "-" * 50,
            body,
        ]
        provenance = stored.get("provenance")
        if provenance is not None:
            lines.append(_provenance_block(provenance))
        lines.append(f"  TIC recomputed from stored peaks: {_fmt_intensity(tic)}")

        logger.info("summarise_reference(%s)", validated.reference)
        return "\n".join(lines)

    @mcp.tool(
        title="Release a data reference",
        annotations=ToolAnnotations(
            read_only_hint=False, idempotent_hint=True, open_world_hint=False
        ),
    )
    def release_reference(
        reference: Annotated[
            str,
            Field(
                min_length=1,
                description="A data reference returned by load_spectrum, e.g. "
                "'ptr:spectrum:1a2b...'.",
            ),
        ],
    ) -> str:
        """Release a server-side data reference you no longer need.

        Frees the stored peaks immediately instead of waiting for the retention
        period to expire.  Releasing is idempotent: releasing an already-released
        or unknown reference reports that it was already gone rather than
        failing, so a retry after a lost response is always safe.
        """
        validated = ReferenceInput(reference=reference)
        removed = reference_store.release(validated.reference)

        if removed:
            logger.info("release_reference(%s): released", validated.reference)
            return (
                f"Released `{validated.reference}`.  The stored peaks are no "
                f"longer available; re-load them with `load_spectrum` if needed."
            )
        return (
            f"`{validated.reference}` was not held by the server (already "
            f"released, expired, or never created).  No action taken."
        )


def _reference_result(
    *,
    reference: DataReference,
    source_path: str,
    fmt: str,
    backend: str,
    spectrum_index: int,
    ms_level: int | None,
    retention_time: float | None,
    precursor_mz: float | None,
    coordinate: list[int] | None,
    mz: np.ndarray,
    tic: float,
) -> SpectrumReferenceResult:
    """Assemble the structured ``load_spectrum`` result."""
    return SpectrumReferenceResult(
        reference=reference.identifier,
        kind=reference.kind.value,
        format=fmt,
        backend=backend,
        source=source_path,
        spectrum_index=spectrum_index,
        ms_level=ms_level,
        retention_time=retention_time,
        precursor_mz=precursor_mz,
        coordinate=coordinate,
        n_peaks=int(mz.size),
        mz_range=([float(mz.min()), float(mz.max())] if mz.size else None),
        tic=tic,
        n_bytes=reference.n_bytes,
        provenance=(reference.provenance.to_dict() if reference.provenance else {}),
    )
