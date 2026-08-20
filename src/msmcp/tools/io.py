"""I/O tools: parse local .mzML / .mgf files into LLM-safe text summaries."""

from __future__ import annotations

import logging
import os
from typing import Any, Iterator

import numpy as np
from pydantic import BaseModel, Field

logger = logging.getLogger("msmcp.tools.io")

# ---------------------------------------------------------------------------
# Try to import the real MassFlow error hierarchy.  If the library isn't
# installed we define a stub so that `except` clauses still compile.
# ---------------------------------------------------------------------------
try:
    from massflow.errors import UnsupportedVendorFormatError
except ImportError:  # pragma: no cover – only hit in dev without massflow

    class UnsupportedVendorFormatError(Exception):
        """Stub for development without massflow installed."""


# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------
class MzMLParseInput(BaseModel):
    """Validated input for the load_mzml_summary tool."""

    file_path: str = Field(
        ...,
        description="Absolute or relative path to a local .mzML or .mgf file.",
    )
    max_spectra: int = Field(
        default=5,
        ge=1,
        le=50,
        description="Maximum number of spectra to include in the summary.",
    )
    noise_threshold: float = Field(
        default=0.0,
        ge=0.0,
        description="Minimum absolute intensity; peaks below this are omitted.",
    )


# ---------------------------------------------------------------------------
# Formatting helpers (token-efficient, fixed precision)
# ---------------------------------------------------------------------------
def _fmt_mz(val: float) -> str:
    """m/z values → 4 decimal places."""
    return f"{val:.4f}"


def _fmt_rt(val: float) -> str:
    """Retention time → 2 decimal places."""
    return f"{val:.2f}"


def _fmt_intensity(val: float) -> str:
    """Intensity → scientific notation above 1e6, otherwise 2 decimal places."""
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

    # -- metadata -----------------------------------------------------------
    idx = _maybe_int(getattr(spectrum, "index", None))
    ms_level = _maybe_int(getattr(spectrum, "ms_level", 1))
    rt = getattr(spectrum, "rt", None)

    lines.append(
        f"Spectrum #{idx}  |  MS{ms_level}  |  RT: {_safe_fmt(rt, _fmt_rt)} min"
    )

    # -- peak arrays --------------------------------------------------------
    mz: np.ndarray | None = _to_float64(getattr(spectrum, "mz", None))
    intensity: np.ndarray | None = _to_float64(getattr(spectrum, "intensity", None))

    if mz is None or intensity is None or len(mz) == 0:
        lines.append("  (no peak data)")
        return "\n".join(lines)

    # Apply noise threshold
    if noise_threshold > 0.0:
        keep = intensity >= noise_threshold
        mz = mz[keep]
        intensity = intensity[keep]

    n_peaks = len(mz)
    lines.append(f"  Peaks (≥{noise_threshold:.1f}): {n_peaks}")

    if n_peaks == 0:
        lines.append("  (all peaks below noise threshold)")
        return "\n".join(lines)

    # Top-N by intensity
    order = np.argsort(intensity)[::-1]  # descending
    show = min(top_n, n_peaks)
    lines.append(f"  Top {show} peaks (m/z → intensity):")
    for i in range(show):
        idx_peak = order[i]
        lines.append(
            f"    {_fmt_mz(mz[idx_peak])} → {_fmt_intensity(intensity[idx_peak])}"
        )

    # Base peak
    bp = np.argmax(intensity)
    lines.append(f"  Base peak: {_fmt_mz(mz[bp])}  ({_fmt_intensity(intensity[bp])})")

    lines.append("")  # blank separator between spectra
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------
def _to_float64(arr: Any) -> np.ndarray | None:
    """Coerce to float64, returning None for falsy / missing input."""
    if arr is None:
        return None
    try:
        out = np.asarray(arr, dtype=np.float64)
        return out if out.ndim == 1 else None
    except (ValueError, TypeError):
        return None


def _maybe_int(val: Any) -> int | str:
    """Return integer string if val is numeric, otherwise '?'."""
    try:
        return int(val)
    except (TypeError, ValueError):
        return "?"


def _safe_fmt(val: Any, fmt_fn: callable) -> str:
    """Apply *fmt_fn* to *val*, returning 'N/A' for None."""
    return "N/A" if val is None else fmt_fn(float(val))


# ---------------------------------------------------------------------------
# Mock spectrum loader (development fallback)
# ---------------------------------------------------------------------------
def _mock_load_spectra(file_path: str) -> Iterator[object]:
    """Yield synthetic spectra when MassFlow is not installed.

    *** This is a development stub.  Real data requires `massflow`. ***
    """
    import random

    class _MockSpectrum:
        __slots__ = ("index", "ms_level", "rt", "mz", "intensity")

        def __init__(self, idx: int) -> None:
            self.index = idx
            self.ms_level = 1
            self.rt = round(idx * 0.5 + random.uniform(0, 0.1), 2)
            n = random.randint(20, 200)
            self.mz = np.sort(np.random.uniform(50.0, 2000.0, n)).astype(np.float64)
            self.intensity = np.random.exponential(1_000.0, n).astype(
                np.float64
            ) * random.uniform(0.1, 10.0)

    logger.warning("Using mock spectrum loader – massflow not installed.")
    for idx in range(6):
        yield _MockSpectrum(idx)


# ===================================================================
# Public tool
# ===================================================================
def register_tools(mcp: Any) -> None:
    """Register I/O tools on the supplied FastMCP *mcp* instance."""

    @mcp.tool()
    def load_mzml_summary(
        file_path: str,
        max_spectra: int = 5,
        noise_threshold: float = 0.0,
    ) -> str:
        """Parse a local .mzML or .mgf file and return a high-level text summary.

        Limits output to the first *max_spectra* spectra to avoid LLM
        context overflow.  Peaks below *noise_threshold* are discarded.
        """
        # --- validate input -------------------------------------------------
        _ = MzMLParseInput(
            file_path=file_path,
            max_spectra=max_spectra,
            noise_threshold=noise_threshold,
        )

        # --- guard against unsupported vendor formats early -----------------
        ext = os.path.splitext(file_path)[1].lower()
        if ext in (".raw", ".d"):
            return _vendor_format_error(ext)

        if ext not in (".mzml", ".mgf"):
            return (
                f"WARNING: Unrecognised file extension '{ext}'. "
                f"Only .mzML and .mgf are officially supported."
            )

        if not os.path.isfile(file_path):
            return f"ERROR: File not found: '{file_path}'"

        # --- load spectra ---------------------------------------------------
        try:
            from massflow.io import load_spectra  # type: ignore[import-untyped]
        except ImportError:
            load_spectra = _mock_load_spectra

        try:
            spectra_iter = load_spectra(file_path)
        except UnsupportedVendorFormatError as exc:
            return _vendor_format_error(ext, exc)
        except Exception as exc:
            logger.error("Failed to load %s: %s", file_path, exc, exc_info=True)
            return f"ERROR: Failed to parse '{file_path}': {exc}"

        # --- build summary --------------------------------------------------
        header = [
            f"File: {os.path.basename(file_path)}",
            f"Format: {ext[1:].upper()}",
            "-" * 50,
        ]

        body: list[str] = []
        count = 0
        for spectrum in spectra_iter:
            if count >= max_spectra:
                break
            body.append(_summarise_spectrum(spectrum, noise_threshold))
            count += 1

        total = count  # may undercount if generator is exhaustive; that's fine

        footer = [
            "-" * 50,
            f"Summarised {count} of {total} total spectra.",
        ]
        if total >= max_spectra:
            footer.append(
                f"(Additional spectra omitted — raise `max_spectra` to see more.)"
            )

        logger.info(
            "load_mzml_summary(file=%r, spectra=%d, noise=%.2f) → %d chars",
            file_path,
            count,
            noise_threshold,
            sum(len(s) for s in header + body + footer),
        )

        return "\n".join(header + body + footer)


# ---------------------------------------------------------------------------
# Shared error messages
# ---------------------------------------------------------------------------
def _vendor_format_error(ext: str, exc: Exception | None = None) -> str:
    hint = f" ({exc})" if exc else ""
    return (
        f"ERROR: Unsupported vendor format '{ext}'.{hint}\n"
        f"Thermo .raw and Agilent/Bruker .d files cannot be parsed directly.\n"
        f"Convert the file to .mzML using ProteoWizard's MSConvert\n"
        f"(https://proteowizard.sourceforge.io/tools/msconvert.html) and retry."
    )
