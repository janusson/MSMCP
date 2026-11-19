"""Source declarations for a bounded subset of PSI-MS acquisition metadata."""

from __future__ import annotations

import math
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from typing import Any, Literal

# Select by accession AND owning mzML element. In particular, a detector's
# resolution is not a scan's mass resolution. Never classify by display name.
SPECTRUM = {"MS:1000511", "MS:1000130", "MS:1000129"}
SCAN = {"MS:1000016", "MS:1000011", "MS:1000800"}
ION = {"MS:1000744", "MS:1000041"}
WINDOW = {"MS:1000827", "MS:1000828", "MS:1000829"}
METHODS = {
    "MS:1000133": "CID",
    "MS:1000422": "beam-type CID",
    "MS:1002481": "higher energy beam-type CID",
    "MS:1000598": "ETD",
}
ACTIVATION = set(METHODS) | {"MS:1000045", "MS:1000138", "MS:1000509"}
COMPONENTS = {
    "source": {"MS:1000398", "MS:1000073", "MS:1002040"},
    "analyzer": {"MS:1000081", "MS:1000084", "MS:1000484", "MS:1000291"},
}


_UNIT_NAMES = {
    "UO:0000266": {"ev", "electronvolt", "electron volt"},
    "UO:0000187": {"percent", "%"},
    "MS:1000040": {"m/z"},
}


@dataclass(frozen=True, slots=True)
class Declaration:
    """Verbatim XML attributes. A declaration alone establishes no measurement."""

    accession: str
    source_name: str | None
    value: str | None
    unit_accession: str | None
    unit_name: str | None
    group_ref: str | None


Terms = tuple[Declaration, ...]


@dataclass(frozen=True, slots=True)
class Quantity:
    value: float | None
    status: Literal[
        "reported",
        "unknown",
        "invalid_value",
        "unknown_unit",
        "multiple_values",
        "unresolved_reference",
    ]


def _quantity(
    terms: Terms, accession: str, unit: str, *, unresolved: bool = False
) -> Quantity:
    """Resolve a single finite nonnegative quantity, keeping the original terms."""
    if unresolved:
        return Quantity(None, "unresolved_reference")
    values: set[float] = set()
    for term in terms:
        if term.accession != accession:
            continue
        if term.unit_accession != unit or (
            term.unit_name and term.unit_name.strip().lower() not in _UNIT_NAMES[unit]
        ):
            return Quantity(None, "unknown_unit")
        try:
            number = float(term.value or "")
        except ValueError:
            return Quantity(None, "invalid_value")
        if not math.isfinite(number) or number < 0:
            return Quantity(None, "invalid_value")
        values.add(number)
    if len(values) > 1:
        return Quantity(None, "multiple_values")
    return Quantity(values.pop(), "reported") if values else Quantity(None, "unknown")


def _bounds(window: Terms) -> tuple[float, float] | None:
    target, lower, upper = (
        _quantity(window, term, "MS:1000040").value
        for term in ("MS:1000827", "MS:1000828", "MS:1000829")
    )
    if target is None or lower is None or upper is None:
        return None
    if target <= 0 or target < lower or not math.isfinite(target + upper):
        return None
    return target - lower, target + upper


@dataclass(frozen=True, slots=True)
class Precursor:
    spectrum_ref: str | None
    source_file_ref: str | None
    external_spectrum_id: str | None
    selected_ions: tuple[Terms, ...]
    isolation_window: Terms
    activation: Terms
    collision_energy_ev: Quantity
    normalized_collision_energy_percent: Quantity
    derived_isolation_bounds_mz: tuple[float, float] | None

    def activation_summary(self) -> str:
        methods = [
            METHODS[t.accession] for t in self.activation if t.accession in METHODS
        ]
        energies = []
        for quantity, unit in (
            (self.collision_energy_ev, "eV"),
            (self.normalized_collision_energy_percent, "% NCE"),
        ):
            if quantity.value is not None:
                energies.append(f"{quantity.value:g} {unit}")
            elif quantity.status != "unknown":
                energies.append(f"unknown ({quantity.status})")
        if not methods and not energies:
            return "unknown"
        return f"{', '.join(methods) or 'method unknown'}, collision energy {', '.join(energies) or 'unknown'}"


@dataclass(frozen=True, slots=True)
class Component:
    kind: str
    order: str | None
    terms: Terms


@dataclass(frozen=True, slots=True)
class Configuration:
    components: tuple[Component, ...]
    scan_settings_ref: str | None


@dataclass(frozen=True, slots=True)
class Scan:
    spectrum_ref: str | None
    source_file_ref: str | None
    external_spectrum_id: str | None
    terms: Terms
    configuration_ref: str | None
    reference_source: Literal["scan", "run"] | None
    configuration: Configuration | None


def _present_fields(fields: list[tuple[str, Any]]) -> dict[str, Any]:
    """Sparse wire form. Preserve ordered scopes, including empty precursors."""
    return {name: value for name, value in fields if value is not None and value != ()}


@dataclass(frozen=True, slots=True)
class InstrumentContext:
    native_id: str | None
    spectrum_terms: Terms
    scans: tuple[Scan, ...]
    precursors: tuple[Precursor, ...]
    unresolved_groups: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {"format": "mzML", **asdict(self, dict_factory=_present_fields)}


@dataclass(frozen=True, slots=True)
class MGFAcquisition:
    """Last block-local source declarations used by the released MGF reader."""

    pepmass: str | None
    mslevel: str | None
    ionmode: str | None
    charge: str | None
    z: str | None
    rtinseconds: str | None
    rtinminutes: str | None

    def to_dict(self) -> dict[str, Any]:
        return {"format": "MGF", **asdict(self)}


SourceAcquisition = InstrumentContext | MGFAcquisition


def activation_summary(context: SourceAcquisition | None) -> str:
    if not isinstance(context, InstrumentContext) or not context.precursors:
        return "unknown"
    rows = [p.activation_summary() for p in context.precursors[:4]]
    if len(context.precursors) > len(rows):
        rows.append(f"{len(context.precursors) - len(rows)} more precursors")
    return " / ".join(rows)


class AcquisitionReader:
    """Per-stream header references, resolved only at their use sites."""

    def __init__(self) -> None:
        self.groups: dict[str, ET.Element] = {}
        self.configurations: dict[str, ET.Element] = {}
        self.run_default: str | None = None

    def header(self, event: str, elem: ET.Element) -> None:
        tag = elem.tag.rsplit("}", 1)[-1]
        if event == "start" and tag == "run":
            self.run_default = elem.get("defaultInstrumentConfigurationRef")
        elif event == "end" and (identifier := elem.get("id")):
            if tag == "referenceableParamGroup":
                self.groups[identifier] = elem
            elif tag == "instrumentConfiguration":
                self.configurations[identifier] = elem

    def terms(self, elem: ET.Element, allowed: set[str], missing: list[str]) -> Terms:
        terms: list[Declaration] = []
        for child in elem:
            tag = child.tag.rsplit("}", 1)[-1]
            group = None
            if tag == "referenceableParamGroupRef":
                group = child.get("ref", "")
                target = self.groups.get(group)
                if target is None:
                    missing.append(group)
                    continue
                params = target.findall("{*}cvParam")
            elif tag == "cvParam":
                params = [child]
            else:
                continue
            for cv in params:
                accession = cv.get("accession", "")
                if accession in allowed:
                    terms.append(
                        Declaration(
                            accession,
                            cv.get("name"),
                            cv.get("value"),
                            cv.get("unitAccession"),
                            cv.get("unitName"),
                            group,
                        )
                    )
        return tuple(terms)

    def scan(self, scan: ET.Element, missing: list[str]) -> Scan:
        ref = scan.get("instrumentConfigurationRef", self.run_default)
        origin: Literal["scan", "run"] | None = None
        if ref is not None:
            origin = "scan" if "instrumentConfigurationRef" in scan.attrib else "run"
        cfg = self.configurations.get(ref) if ref is not None else None
        configuration = None
        if cfg is not None:
            components = []
            for component in cfg.findall("{*}componentList/*"):
                kind = component.tag.rsplit("}", 1)[-1]
                if kind in COMPONENTS:
                    components.append(
                        Component(
                            kind,
                            component.get("order"),
                            self.terms(component, COMPONENTS[kind], missing),
                        )
                    )
            configuration = Configuration(tuple(components), cfg.get("scanSettingsRef"))
        return Scan(
            scan.get("spectrumRef"),
            scan.get("sourceFileRef"),
            scan.get("externalSpectrumID"),
            self.terms(scan, SCAN, missing),
            ref,
            origin,
            configuration,
        )

    def spectrum(self, elem: ET.Element) -> InstrumentContext:
        missing: list[str] = []
        spectrum_terms = self.terms(elem, SPECTRUM, missing)
        scans = tuple(
            self.scan(s, missing) for s in elem.findall("{*}scanList/{*}scan")
        )
        precursors = []
        for p in elem.findall("{*}precursorList/{*}precursor"):
            ions = tuple(
                self.terms(i, ION, missing)
                for i in p.findall("{*}selectedIonList/{*}selectedIon")
            )
            missing_window: list[str] = []
            window = tuple(
                t
                for w in p.findall("{*}isolationWindow")
                for t in self.terms(w, WINDOW, missing_window)
            )
            missing_activation: list[str] = []
            activation = tuple(
                t
                for a in p.findall("{*}activation")
                for t in self.terms(a, ACTIVATION, missing_activation)
            )
            missing.extend(missing_window)
            missing.extend(missing_activation)
            precursors.append(
                Precursor(
                    p.get("spectrumRef"),
                    p.get("sourceFileRef"),
                    p.get("externalSpectrumID"),
                    ions,
                    window,
                    activation,
                    _quantity(
                        activation,
                        "MS:1000045",
                        "UO:0000266",
                        unresolved=bool(missing_activation),
                    ),
                    _quantity(
                        activation,
                        "MS:1000138",
                        "UO:0000187",
                        unresolved=bool(missing_activation),
                    ),
                    None if missing_window else _bounds(window),
                )
            )
        return InstrumentContext(
            elem.get("id"), spectrum_terms, scans, tuple(precursors), tuple(missing)
        )
