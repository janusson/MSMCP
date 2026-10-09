"""Instrument-class defaults for the experimental constants MSMCP applies.

Audit F7 (``docs/audit/2026-09-22-standard-audit.md``) found that the
thresholds which encode the experimental question were module-level literals:
a 5.0 ppm precursor gate, a 0.02 Da peak-match window, a 0.02 Da diagnostic-ion
window, SNR cut-offs of 5 and 20, and an FDR threshold / small-library floor of
0.05 / 2000.  The consequence is that an analysis cannot be re-run under
different instrument assumptions without editing source, and the value that was
*actually used* is invisible in the result it produced.

This module is the single home for those constants.  Each instrument class
carries its own documented defaults, the default class reproduces MSMCP's v1.0
values exactly (so nothing changes unless a caller asks for a class), and every
value travels into the result that used it — the report body for the
string-returning tools, and ``Provenance.parameters`` for ``search_library``.

What these numbers are, and are not
-----------------------------------
They are *documented conventions*, not measured acquisition parameters.  MSMCP
does not yet parse instrument model, resolution or calibration from a file
(audit F6, ``InstrumentContext`` is the item that will), so a per-class table is
the most honest thing available: it states the assumption a result was produced
under instead of hiding a bare literal, and it is overridable per call.  A
caller who knows the real acquisition should pass it; a caller who does not gets
the conservative default and a result that says which class it used.

Constant provenance (the basis for the defaults)
------------------------------------------------
``precursor_tolerance``
    The precursor gate is expressed in ppm only where the instrument class
    resolves mass accurately enough for ppm to mean something.  Orbitrap- and
    TOF-class instruments quote mass accuracy in ppm (a few ppm and 5-15 ppm
    respectively); ion-trap and triple-quadrupole instruments are
    unit-resolution, where a fixed ppm window shrinks below one nominal mass
    below a few kDa and is therefore replaced with a Da window.
``ms2_tolerance_da`` / ``diagnostic_ion_tolerance_da``
    Fragment and diagnostic-ion matching windows follow the same resolution
    logic: 0.02 Da for the generic default, tighter for accurate-mass classes,
    and a unit-mass window (0.5-0.7 Da) for unit-resolution classes.
``snr_low`` / ``snr_high``
    The 5 / 20 SNR cut-offs are MSMCP's v1.0 reporting bands, shared across
    classes until there is per-class noise data to justify otherwise.
``fdr_threshold`` / ``small_library_threshold``
    0.05 is the conventional Benjamini-Hochberg FDR threshold; 2000 is the
    library size below which MSMCP switches from the FDR branch to the
    empirical p-value branch (audit F12), i.e. a property of the statistics,
    not of the instrument, and shared across classes.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final, Literal

__all__ = [
    "DEFAULT_INSTRUMENT_CLASS",
    "INSTRUMENT_CLASS_NAMES",
    "InstrumentClass",
    "InstrumentProfile",
    "Tolerance",
    "get_profile",
]


InstrumentClass = Literal["generic", "orbitrap", "tof", "ion_trap", "triple_quad"]
"""The instrument classes MSMCP ships defaults for."""


@dataclass(frozen=True, slots=True)
class Tolerance:
    """A match window together with its unit.

    Keeping the unit explicit is the point: a ppm window is meaningless on a
    unit-resolution instrument, so a profile declares which unit its precursor
    gate is in and the tools report the value in that unit rather than
    converting one into the other.
    """

    value: float
    unit: Literal["ppm", "Da"]

    def __post_init__(self) -> None:
        if self.value <= 0.0:
            raise ValueError(f"tolerance must be positive, got {self.value!r}")
        if self.unit not in ("ppm", "Da"):
            raise ValueError(f"unknown tolerance unit {self.unit!r}")

    def describe(self) -> str:
        """Render the window as it appears in a report, e.g. ``"5.0 ppm"``."""
        text = f"{self.value:.1f}"
        if float(text) != self.value:  # needs more precision than one decimal
            text = f"{self.value:g}"
        return f"{text} {self.unit}"

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-safe mapping for the provenance record."""
        return {"value": self.value, "unit": self.unit}


@dataclass(frozen=True, slots=True)
class InstrumentProfile:
    """The documented defaults one instrument class contributes.

    ``provenance`` states the basis for every number in the profile, so a
    result can be read back against the assumption it was produced under (the
    F7 requirement that a value moved out of a literal must carry where its
    default came from).
    """

    instrument_class: str
    precursor_tolerance: Tolerance
    ms2_tolerance_da: float
    diagnostic_ion_tolerance_da: float
    snr_low: float
    snr_high: float
    fdr_threshold: float
    small_library_threshold: int
    provenance: str

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-safe mapping for ``Provenance.parameters``."""
        return {
            "instrument_class": self.instrument_class,
            "precursor_tolerance": self.precursor_tolerance.to_dict(),
            "ms2_tolerance_da": self.ms2_tolerance_da,
            "diagnostic_ion_tolerance_da": self.diagnostic_ion_tolerance_da,
            "snr_low": self.snr_low,
            "snr_high": self.snr_high,
            "fdr_threshold": self.fdr_threshold,
            "small_library_threshold": self.small_library_threshold,
        }


_GENERIC_PROVENANCE = (
    "MSMCP v1.0 defaults — the literals audit F7 replaced: a 5.0 ppm precursor "
    "gate, a 0.02 Da MS2 window, a 0.02 Da diagnostic-ion window, SNR cut-offs "
    "of 5 and 20, a 0.05 Benjamini-Hochberg FDR threshold and a 2000-spectrum "
    "FDR small-library floor.  Retained as the default so existing analyses and "
    "reports stay reproducible; it is a convention, not an instrument claim."
)

_ORBITRAP_PROVENANCE = (
    "High-resolution accurate-mass class.  A 5.0 ppm precursor gate is the "
    "conservative end of the few-ppm external-calibration accuracy typical of "
    "Orbitrap-class instruments; the MS2 and diagnostic-ion windows tighten to "
    "0.01 Da to match that resolution.  SNR cut-offs and the FDR / "
    "small-library thresholds follow the shared defaults (they are properties "
    "of the statistics and the v1.0 reporting bands, not of the mass analyser)."
)

_TOF_PROVENANCE = (
    "Time-of-flight accurate-mass class.  Reflectron TOF instruments typically "
    "quote 5-15 ppm, so the precursor gate is 15.0 ppm and the MS2 / "
    "diagnostic-ion windows widen to 0.05 Da.  SNR cut-offs and the FDR / "
    "small-library thresholds are shared."
)

_ION_TRAP_PROVENANCE = (
    "Unit-resolution ion-trap class.  Mass accuracy is not quoted in ppm, so "
    "the precursor gate is a 0.5 Da window, as are the MS2 and diagnostic-ion "
    "windows — a 5 ppm gate is meaningless on an instrument that cannot resolve "
    "fractions of a nominal mass.  SNR cut-offs and the FDR / small-library "
    "thresholds are shared."
)

_TRIPLE_QUAD_PROVENANCE = (
    "Unit-resolution tandem (triple-quadrupole) class.  A 0.7 Da window is the "
    "conventional unit-mass tolerance for precursor, fragment and "
    "diagnostic-ion matching.  SNR cut-offs and the FDR / small-library "
    "thresholds are shared."
)

_PROFILES: dict[str, InstrumentProfile] = {
    "generic": InstrumentProfile(
        instrument_class="generic",
        precursor_tolerance=Tolerance(5.0, "ppm"),
        ms2_tolerance_da=0.02,
        diagnostic_ion_tolerance_da=0.02,
        snr_low=5.0,
        snr_high=20.0,
        fdr_threshold=0.05,
        small_library_threshold=2000,
        provenance=_GENERIC_PROVENANCE,
    ),
    "orbitrap": InstrumentProfile(
        instrument_class="orbitrap",
        precursor_tolerance=Tolerance(5.0, "ppm"),
        ms2_tolerance_da=0.01,
        diagnostic_ion_tolerance_da=0.01,
        snr_low=5.0,
        snr_high=20.0,
        fdr_threshold=0.05,
        small_library_threshold=2000,
        provenance=_ORBITRAP_PROVENANCE,
    ),
    "tof": InstrumentProfile(
        instrument_class="tof",
        precursor_tolerance=Tolerance(15.0, "ppm"),
        ms2_tolerance_da=0.05,
        diagnostic_ion_tolerance_da=0.05,
        snr_low=5.0,
        snr_high=20.0,
        fdr_threshold=0.05,
        small_library_threshold=2000,
        provenance=_TOF_PROVENANCE,
    ),
    "ion_trap": InstrumentProfile(
        instrument_class="ion_trap",
        precursor_tolerance=Tolerance(0.5, "Da"),
        ms2_tolerance_da=0.5,
        diagnostic_ion_tolerance_da=0.5,
        snr_low=5.0,
        snr_high=20.0,
        fdr_threshold=0.05,
        small_library_threshold=2000,
        provenance=_ION_TRAP_PROVENANCE,
    ),
    "triple_quad": InstrumentProfile(
        instrument_class="triple_quad",
        precursor_tolerance=Tolerance(0.7, "Da"),
        ms2_tolerance_da=0.7,
        diagnostic_ion_tolerance_da=0.7,
        snr_low=5.0,
        snr_high=20.0,
        fdr_threshold=0.05,
        small_library_threshold=2000,
        provenance=_TRIPLE_QUAD_PROVENANCE,
    ),
}

PROFILES: Mapping[str, InstrumentProfile] = MappingProxyType(_PROFILES)
"""Every supported instrument class, keyed by name (read-only)."""

DEFAULT_INSTRUMENT_CLASS: Final[InstrumentClass] = "generic"
"""The class used when a caller does not name one: MSMCP's v1.0 behaviour."""

INSTRUMENT_CLASS_NAMES: Final[tuple[str, ...]] = tuple(sorted(PROFILES))
"""The instrument-class names accepted on the wire, sorted."""


def get_profile(instrument_class: str | None = None) -> InstrumentProfile:
    """Resolve *instrument_class* to its documented profile.

    ``None`` resolves to :data:`DEFAULT_INSTRUMENT_CLASS`, so a caller that does
    not know (or care about) the instrument gets the v1.0 defaults.  An unknown
    name raises rather than silently falling back, because a misspelled class
    must not change an analysis without saying so.
    """
    name = DEFAULT_INSTRUMENT_CLASS if instrument_class is None else instrument_class
    try:
        return PROFILES[name]
    except KeyError:
        known = ", ".join(INSTRUMENT_CLASS_NAMES)
        raise ValueError(
            f"unknown instrument class {name!r}; expected one of: {known}"
        ) from None
