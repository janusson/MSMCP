"""Independent, pinned source XML checks and adversarial metadata regressions."""

from __future__ import annotations

import hashlib
import json
import xml.etree.ElementTree as ET
from collections.abc import Callable
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from msmcp import ingest, mgf, mzml, security
from msmcp.acquisition import InstrumentContext, MGFAcquisition, activation_summary
from msmcp.tools import io, qc, search

DATA = Path(__file__).parent / "data" / "acquisition"
LET7 = DATA / "let7.mzML"
YEAST = DATA / "yeast.mzML"
NS = "{http://psi.hupo.org/ms/mzml}"


def context(path: Path) -> InstrumentContext:
    record = next(mzml.iter_spectra(path)).acquisition
    assert isinstance(record, InstrumentContext)
    return record


@pytest.mark.parametrize(
    ("path", "digest", "energy", "rt"),
    [
        (
            LET7,
            "77da1ed84210d5ef6fdec15304f1bb6ffbb4259117fba5e8c1e2a17326e8080a",
            22,
            14.10438,
        ),
        (
            YEAST,
            "e281957d38c443e9459ba86d717eb06b17fd90b3f27afe74d2fac2cc160c62ea",
            None,
            2655.095703125,
        ),
    ],
)
def test_source_oracle(path: Path, digest: str, energy: int | None, rt: float) -> None:
    assert hashlib.sha256(path.read_bytes()).hexdigest() == digest
    # Independent XPath against upstream bytes. Select a specific spectrum and
    # precursor, so a match in another scan cannot satisfy the check.
    root = ET.parse(path)
    spec = root.find(".//{*}spectrum[@index='0']")
    assert spec is not None
    activation = spec.find("{*}precursorList/{*}precursor/{*}activation")
    assert activation is not None
    terms = [e for e in activation.findall("{*}cvParam")]
    assert any(t.get("accession") == "MS:1000422" for t in terms)
    ev = activation.find("{*}cvParam[@accession='MS:1000045']")
    if energy is None:
        assert ev is None
        assert (
            activation.find("{*}userParam[@name='collision energy'][@value='45']")
            is not None
        )
    else:
        assert ev is not None
        assert (ev.get("value"), ev.get("unitAccession")) == ("22", "UO:0000266")
    record = context(path)
    p = record.precursors[0]
    assert [(t.accession, t.value, t.unit_accession) for t in p.activation] == [
        (t.get("accession"), t.get("value"), t.get("unitAccession")) for t in terms
    ]
    assert p.collision_energy_ev.value == energy
    assert p.normalized_collision_energy_percent.status == "unknown"
    scan = record.scans[0]
    assert scan.reference_source == "run" and scan.configuration_ref == "ic_0"
    assert scan.configuration
    assert "MS:1000484" in [
        t.accession for c in scan.configuration.components for t in c.terms
    ]
    assert scan.terms[0].unit_accession == "UO:0000010"
    assert float(scan.terms[0].value or "") == rt
    assert next(mzml.iter_spectra(path)).retention_time == pytest.approx(rt / 60)
    if path == LET7:
        assert p.derived_isolation_bounds_mz == pytest.approx(
            (2262.920361340041, 2264.320361316199)
        )
        assert [t.value for t in p.selected_ions[0]] == ["2263.62036132812", "3"]


def test_scoped_groups_multiple_scans_and_precursors(tmp_path: Path) -> None:
    tree = ET.parse(LET7)
    root = tree.getroot()
    groups = ET.SubElement(root, NS + "referenceableParamGroupList")
    root.remove(groups)
    root.insert(2, groups)
    group = ET.SubElement(groups, NS + "referenceableParamGroup", id="energy")
    ET.SubElement(
        group,
        NS + "cvParam",
        accession="MS:1000138",
        value="32",
        unitAccession="UO:0000187",
    )
    scanlist = root.find(".//{*}scanList")
    plist = root.find(".//{*}precursorList")
    assert scanlist is not None and plist is not None
    second = deepcopy(plist[0])
    plist.append(second)
    activation = second.find("{*}activation")
    assert activation is not None
    ionlist = second.find("{*}selectedIonList")
    assert ionlist is not None
    ion = ET.SubElement(ionlist, NS + "selectedIon")
    ET.SubElement(ion, NS + "cvParam", accession="MS:1000744", value="100")
    activation.clear()
    ET.SubElement(activation, NS + "referenceableParamGroupRef", ref="energy")
    ET.SubElement(
        activation,
        NS + "cvParam",
        accession="MS:9999999",
        name="collision energy",
        value="90",
    )
    configs = root.find("{*}instrumentConfigurationList")
    assert configs is not None
    alt = ET.SubElement(configs, NS + "instrumentConfiguration", id="IC2")
    component = ET.SubElement(
        ET.SubElement(alt, NS + "componentList"), NS + "analyzer", order="2"
    )
    ET.SubElement(component, NS + "cvParam", accession="MS:1000084")
    ET.SubElement(scanlist, NS + "scan", instrumentConfigurationRef="IC2")
    third_scan = ET.SubElement(
        scanlist, NS + "scan", instrumentConfigurationRef="missing"
    )
    ET.SubElement(third_scan, NS + "referenceableParamGroupRef", ref="unresolved")
    path = tmp_path / "multiple.mzML"
    tree.write(path)
    c = context(path)
    assert [s.reference_source for s in c.scans] == ["run", "scan", "scan"]
    assert c.scans[1].configuration is not None
    assert [t.accession for t in c.scans[1].configuration.components[0].terms] == [
        "MS:1000084"
    ]
    assert c.scans[2].configuration is None
    assert c.unresolved_groups == ("unresolved",)
    first, last = c.precursors
    assert [i[0].value for i in last.selected_ions] == ["2263.62036132812", "100"]
    assert first.collision_energy_ev.value == 22
    assert last.collision_energy_ev.value is None
    assert last.normalized_collision_energy_percent.value == 32
    assert [t.group_ref for t in last.activation] == ["energy"]
    assert (
        activation_summary(c)
        == "beam-type CID, collision energy 22 eV / method unknown, collision energy 32 % NCE"
    )


@pytest.mark.parametrize(
    ("raw", "unit", "duplicate", "status"),
    [
        ("NaN", "UO:0000266", False, "invalid_value"),
        ("bad", "UO:0000266", False, "invalid_value"),
        ("30", "UO:0000187", False, "unknown_unit"),
        ("30", "", False, "unknown_unit"),
        ("30", "UO:0000266", True, "multiple_values"),
    ],
)
def test_unusable_energy(
    tmp_path: Path, raw: str, unit: str, duplicate: bool, status: str
) -> None:
    tree = ET.parse(LET7)
    a = tree.find(".//{*}activation")
    assert a is not None
    e = a.find("{*}cvParam[@accession='MS:1000045']")
    assert e is not None
    if duplicate:
        a.append(deepcopy(e))
    e.set("unitAccession", unit)
    e.set("value", raw)
    path = tmp_path / "changed.mzML"
    tree.write(path)
    p = context(path).precursors[0]
    assert p.collision_energy_ev.value is None
    assert p.collision_energy_ev.status == status


def test_sparse_mgf(tmp_path: Path) -> None:
    p = tmp_path / "sample.mgf"
    p.write_text("BEGIN IONS\nPEPMASS=170 123\nCHARGE=2-\n55 5\nEND IONS\n")
    spec = next(mgf.iter_spectra(p))
    assert spec.precursor_mz == 170 and spec.charge == 2 and spec.polarity is None
    assert isinstance(spec.acquisition, MGFAcquisition)
    assert spec.acquisition.pepmass == "170 123" and spec.acquisition.charge == "2-"
    assert spec.acquisition.mslevel is None and spec.acquisition.ionmode is None
    assert "unknown" in activation_summary(spec.acquisition)


def test_load_reference_qc_and_search_reports(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    io_tools: dict[str, Callable[..., Any]],
    qc_tools: dict[str, Callable[..., str]],
) -> None:
    for module in (io, qc, ingest, security):
        monkeypatch.setattr(
            module, "DEFAULT_POLICY", security.SecurityPolicy(allowed_root=tmp_path)
        )
    path = tmp_path / "query.mzML"
    path.write_bytes(LET7.read_bytes())
    loaded = io_tools["load_spectrum"](str(path))
    data = json.loads(loaded.model_dump_json())
    record = data["provenance"]["source_acquisition"]
    assert record["precursors"][0]["collision_energy_ev"] == {
        "value": 22,
        "status": "reported",
    }
    assert "energy 22 eV" in qc_tools["generate_qc_summary"](str(path))
    assert "energy 22 eV" in io_tools["summarise_reference"](loaded.reference)
    library = tmp_path / "library.msp"
    library.write_text("Name: test\nNum Peaks: 1\n250 10\n")
    ctx = search._reference_context(loaded.reference)
    result = search._run_scan(
        search.SearchRequest(
            database_file=str(library),
            experimental_peaks=((250.0, 10.0),),
            spectrum_reference=loaded.reference,
            query_context=ctx,
        )
    )
    assert "beam-type CID, collision energy 22 eV" in result.report
    assert "Library activation comparability: unknown" in result.report
    assert (
        json.loads(json.dumps(result.provenance.to_dict()))["source_acquisition"]
        == record
    )


def test_upstream_conversion_without_instrument_components() -> None:
    path = DATA / "sparse-conversion.mzML"
    assert (
        hashlib.sha256(path.read_bytes()).hexdigest()
        == "c5f4d98e9d0b4ca6d8df49a309dfd5e237b2eeac340f3587278ffce5617c1935"
    )
    xml = ET.parse(path)
    assert xml.find(".//{*}componentList") is None
    assert xml.find(".//{*}activation") is None
    record = context(path)
    assert not record.precursors
    assert activation_summary(record) == "unknown"
    cfg = record.scans[0].configuration
    assert cfg is not None and not cfg.components


def test_unresolved_activation_and_asymmetric_window(tmp_path: Path) -> None:
    tree = ET.parse(LET7)
    act = tree.find(".//{*}precursor/{*}activation")
    upper = tree.find(".//{*}isolationWindow/{*}cvParam[@accession='MS:1000829']")
    assert act is not None and upper is not None
    ET.SubElement(act, NS + "referenceableParamGroupRef", ref="missing")
    upper.set("value", "2")
    path = tmp_path / "partial.mzML"
    tree.write(path)
    p = context(path).precursors[0]
    assert p.collision_energy_ev.status == "unresolved_reference"
    assert p.collision_energy_ev.value is None
    assert p.derived_isolation_bounds_mz == pytest.approx(
        (2262.920361340041, 2265.62036132812)
    )
