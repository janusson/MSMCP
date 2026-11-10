"""Independent spectrum-level qualification of the optional Numpress reader.

Numpress bytes and expected values come only from the frozen original-reference
JSON, never from the decoder under test or a pynumpress encoder. Codec arithmetic,
compressed-stream structure and native reservation bounds belong to the companion
qualification lane; these tests exercise scientific identity and ingestion.
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import importlib
import json
import sys
import xml.etree.ElementTree as ET
import zlib
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal, TypedDict, cast

import numpy as np
import pytest

from msmcp import security
from msmcp.errors import MalformedFileError, MissingDependencyError
from msmcp.ingest import Source, resolve_source
from msmcp.mzml import Spectrum
from msmcp.security import FileSizeExceededError, SecurityPolicy
from msmcp.state import store as reference_store
from msmcp.state.pointers import PointerStore
from msmcp.tools import io


class _Vector(TypedDict):
    name: str
    codec: Literal["linear", "pic", "slof"]
    hex: str
    decoded: list[float]


class _Fixture(TypedDict):
    vectors: list[_Vector]


_FIXTURE = cast(
    _Fixture,
    json.loads(
        (Path(__file__).parent / "data" / "numpress_known_answers.json").read_text()
    ),
)
_VECTORS = {vector["name"]: vector for vector in _FIXTURE["vectors"]}
_TERMS = {
    "linear": ("MS:1002312", "MS:1002746"),
    "pic": ("MS:1002313", "MS:1002747"),
    "slof": ("MS:1002314", "MS:1002748"),
}
_MZ = "MS:1000514"
_INTENSITY = "MS:1000515"
# Duplicate and descending masses, plus zero intensities, must not be sorted,
# deduplicated or filtered independently of their partners.
_PLAIN_MZ = [103.0, 100.0, 101.0, 100.0, 102.0, 104.0]
_PLAIN_INTENSITY = [7.0, 0.0, 2.0, 7.0]


def _cv(parent: ET.Element, accession: str, **attributes: str) -> None:
    ET.SubElement(parent, "cvParam", {"accession": accession, **attributes})


def _array(
    role: str,
    vector_name: str | None,
    *,
    values: list[float] | None = None,
    compression: str = "plain",
) -> ET.Element:
    array = ET.Element("binaryDataArray")
    _cv(array, role)
    _cv(array, "MS:1000523")
    if vector_name is None:
        assert values is not None
        raw = np.asarray(values, dtype="<f8").tobytes()
        _cv(array, "MS:1000576")
    else:
        vector = _VECTORS[vector_name]
        plain, combined = _TERMS[vector["codec"]]
        _cv(array, combined if compression == "combined-zlib" else plain)
        if compression == "separate-zlib":
            _cv(array, "MS:1000574")
        raw = bytes.fromhex(vector["hex"])
        if compression != "plain":
            raw = zlib.compress(raw)
    payload = base64.b64encode(raw).decode("ascii")
    array.set("encodedLength", str(len(payload)))
    ET.SubElement(array, "binary").text = payload
    return array


def _scan(
    mz_vector: str | None = "pic_half_up_rounding",
    intensity_vector: str | None = "slof_quantization",
    *,
    index: int = 17,
    native_id: str = "controllerType=0 controllerNumber=1 scan=42",
    declared: int | None = None,
    compression: str = "plain",
    reverse_arrays: bool = False,
) -> ET.Element:
    size = len(_VECTORS[mz_vector]["decoded"] if mz_vector else _PLAIN_MZ)
    scan = ET.Element(
        "spectrum",
        index=str(index),
        id=native_id,
        defaultArrayLength=str(size if declared is None else declared),
    )
    arrays = ET.SubElement(scan, "binaryDataArrayList", count="2")
    pair = [
        _array(_MZ, mz_vector, values=_PLAIN_MZ, compression=compression),
        _array(
            _INTENSITY,
            intensity_vector,
            values=_PLAIN_INTENSITY,
            compression=compression,
        ),
    ]
    arrays.extend(reversed(pair) if reverse_arrays else pair)
    return scan


def _write_source(
    tmp_path: Path,
    scans: list[ET.Element],
    *,
    outer_gzip: bool = False,
    policy: SecurityPolicy | None = None,
) -> Source:
    root = ET.Element("mzML", xmlns="http://psi.hupo.org/ms/mzml")
    run = ET.SubElement(root, "run", id="qualification")
    spectra = ET.SubElement(run, "spectrumList", count=str(len(scans)))
    spectra.extend(scans)
    raw = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    path = tmp_path / ("integrity.mzML.gz" if outer_gzip else "integrity.mzML")
    path.write_bytes(gzip.compress(raw, mtime=0) if outer_gzip else raw)
    effective = policy if policy is not None else SecurityPolicy(allowed_root=tmp_path)
    return resolve_source(path, effective)


def _assert_arrays(spectrum: Spectrum, mz: list[float], intensity: list[float]) -> None:
    assert spectrum.n_peaks == len(mz) == len(intensity)
    for actual, expected in ((spectrum.mz, mz), (spectrum.intensity, intensity)):
        assert actual.shape == (len(expected),)
        assert actual.dtype == np.dtype("float64")
        np.testing.assert_array_max_ulp(actual, np.asarray(expected), maxulp=4)
        assert not actual.flags.writeable
        if actual.size:
            with pytest.raises(ValueError, match="read-only"):
                actual[0] = -1.0
    assert spectrum.tic == pytest.approx(sum(intensity), rel=1e-14, abs=1e-14)


@pytest.mark.parametrize("reverse_arrays", [False, True], ids=["mz-first", "int-first"])
@pytest.mark.parametrize(
    ("mz_vector", "intensity_vector"),
    [
        ("linear_no_padding", None),
        (None, "pic_half_up_rounding"),
        (None, "slof_quantization"),
        ("pic_half_up_rounding", "slof_quantization"),
    ],
    ids=["linear-plain", "plain-pic", "plain-slof", "pic-slof"],
)
def test_numpress_preserves_peak_pairing_order_and_buffers(
    tmp_path: Path,
    mz_vector: str | None,
    intensity_vector: str | None,
    reverse_arrays: bool,
) -> None:
    source = _write_source(
        tmp_path,
        [_scan(mz_vector, intensity_vector, reverse_arrays=reverse_arrays)],
    )
    spectra = list(source.iter_spectra())
    assert len(spectra) == 1
    _assert_arrays(
        spectra[0],
        _VECTORS[mz_vector]["decoded"] if mz_vector else _PLAIN_MZ,
        _VECTORS[intensity_vector]["decoded"] if intensity_vector else _PLAIN_INTENSITY,
    )
    assert spectra[0].source_warnings == ()


def test_numpress_identity_metadata_and_tool_provenance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    io_tools: dict[str, Callable[..., Any]],
) -> None:
    first = _scan(index=90, native_id="scan=900")
    scan = _scan(index=7, native_id="controllerType=0 controllerNumber=1 scan=42")
    _cv(scan, "MS:1000511", value="2")
    _cv(scan, "MS:1000129")
    _cv(scan, "MS:1000127")
    scans = ET.SubElement(scan, "scanList", count="1")
    _cv(
        ET.SubElement(scans, "scan"),
        "MS:1000016",
        value="123.0",
        unitAccession="UO:0000010",
        unitName="second",
    )
    precursor = ET.SubElement(
        ET.SubElement(scan, "precursorList", count="1"), "precursor"
    )
    window = ET.SubElement(precursor, "isolationWindow")
    _cv(window, "MS:1000828", value="0.4")
    _cv(window, "MS:1000829", value="0.6")
    ion = ET.SubElement(
        ET.SubElement(precursor, "selectedIonList", count="1"), "selectedIon"
    )
    _cv(ion, "MS:1000744", value="445.1234")
    _cv(ion, "MS:1000041", value="3")
    source = _write_source(tmp_path, [first, scan], outer_gzip=True)
    monkeypatch.setattr(io, "DEFAULT_POLICY", source.policy)
    monkeypatch.setattr(reference_store, "_STORE", PointerStore())

    result = cast(
        io.SpectrumReferenceResult,
        io_tools["load_spectrum"](str(source.path), spectrum_index=1),
    )
    stored = reference_store.resolve_spectrum(result.reference)
    assert (stored.index, stored.native_id) == (7, scan.get("id"))
    assert stored.ms_level == 2
    assert stored.retention_time == pytest.approx(2.05)
    assert stored.retention_time_source_value == 123.0
    assert stored.retention_time_source_unit_accession == "UO:0000010"
    assert stored.retention_time_source_unit_name == "second"
    assert stored.precursor_mz == 445.1234
    assert stored.charge == 3
    assert stored.isolation_window == 1.0
    assert stored.polarity == "negative"
    assert stored.representation == "centroid"
    assert stored.coordinate is None
    assert stored.source_warnings == ()
    _assert_arrays(
        stored,
        _VECTORS["pic_half_up_rounding"]["decoded"],
        _VECTORS["slof_quantization"]["decoded"],
    )
    assert result.spectrum_index == 1  # ordinal, not spectrum/@index or native scan id
    assert result.source == str(source.path)
    assert (result.format, result.backend) == ("mzML", "msmcp.mzml")
    assert result.provenance["operation"] == "load_spectrum"
    assert result.provenance["parameters"] == {
        "spectrum_index": 1,
        "source_format": "mzML",
        "reader_backend": "msmcp.mzml",
    }
    assert result.provenance["sources"] == [
        {
            "path": str(source.path),
            "format": "mzML",
            "backend": "msmcp.mzml",
            "size_bytes": source.path.stat().st_size,
            "digest": "sha256:" + hashlib.sha256(source.path.read_bytes()).hexdigest(),
        }
    ]
    assert reference_store.reference_summary(result.reference)["provenance"] == (
        result.provenance
    )


def test_numpress_does_not_invent_absent_metadata(tmp_path: Path) -> None:
    scan = _scan()
    del scan.attrib["index"]
    del scan.attrib["id"]
    spectrum = next(_write_source(tmp_path, [scan]).iter_spectra())
    for field in (
        "index",
        "native_id",
        "ms_level",
        "retention_time",
        "retention_time_source_value",
        "retention_time_source_unit_accession",
        "retention_time_source_unit_name",
        "precursor_mz",
        "charge",
        "isolation_window",
        "polarity",
        "representation",
        "coordinate",
    ):
        assert getattr(spectrum, field) is None, field


def test_nonempty_length_mismatches_warn_once_and_preserve_decoded_data(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    source = _write_source(
        tmp_path,
        [_scan(index=8, declared=5), _scan(index=2, declared=7), _scan(index=21)],
    )
    with caplog.at_level("WARNING", logger="msmcp.mzml"):
        spectra = list(source.iter_spectra())
    assert [s.index for s in spectra] == [8, 2, 21]
    assert [s.source_warnings for s in spectra] == [
        ("declared_array_length_mismatch",),
        ("declared_array_length_mismatch",),
        (),
    ]
    for spectrum in spectra:
        _assert_arrays(
            spectrum,
            _VECTORS["pic_half_up_rounding"]["decoded"],
            _VECTORS["slof_quantization"]["decoded"],
        )
    warnings = [r.getMessage() for r in caplog.records if r.name == "msmcp.mzml"]
    assert len(warnings) == 1
    assert "2 spectrum/spectra" in warnings[0]
    assert "nothing was refused" in warnings[0]
    assert str(source.path) in warnings[0]


@pytest.mark.parametrize(
    "empty_form", ["reference", "pic-reference", "self-closing", "absent"]
)
def test_numpress_empty_middle_scan_is_retained_and_iteration_continues(
    tmp_path: Path, empty_form: str
) -> None:
    empty = _scan(
        "pic_empty" if empty_form == "pic-reference" else "linear_empty",
        "slof_empty",
        index=1,
        native_id="scan=empty",
    )
    if empty_form in {"self-closing", "absent"}:
        for array in empty.findall("binaryDataArrayList/binaryDataArray"):
            binary = array.find("binary")
            assert binary is not None
            if empty_form == "absent":
                array.remove(binary)
            else:
                binary.text = None
            array.set("encodedLength", "0")
    source = _write_source(tmp_path, [_scan(index=0), empty, _scan(index=2)])
    spectra = list(source.iter_spectra())
    assert [s.index for s in spectra] == [0, 1, 2]
    assert spectra[1].native_id == "scan=empty"
    _assert_arrays(spectra[1], [], [])
    assert spectra[1].source_warnings == ()
    assert spectra[2].n_peaks == 6


@pytest.mark.parametrize(
    ("mz_vector", "intensity_vector"),
    [("linear_empty", "slof_empty"), ("pic_empty", "slof_empty")],
    ids=["encoded-empty", "empty-pic-payload"],
)
def test_numpress_declared_nonempty_but_empty_arrays_are_refused(
    tmp_path: Path, mz_vector: str, intensity_vector: str
) -> None:
    source = _write_source(tmp_path, [_scan(mz_vector, intensity_vector, declared=1)])
    with pytest.raises(MalformedFileError, match="declares 1 peaks"):
        list(source.iter_spectra())


@pytest.mark.parametrize("role", [_MZ, _INTENSITY], ids=["mz", "intensity"])
@pytest.mark.parametrize("duplicate_first", [False, True], ids=["last", "first"])
def test_duplicate_numpress_array_roles_are_refused(
    tmp_path: Path, role: str, duplicate_first: bool
) -> None:
    scan = _scan()
    arrays = scan.find("binaryDataArrayList")
    assert arrays is not None
    # Both candidates have six values but different values/codecs. Last-wins
    # acceptance changes scientific data when XML sibling order changes.
    duplicate = _array(role, "slof_uint16_byte_order")
    arrays.insert(0 if duplicate_first else len(arrays), duplicate)
    arrays.set("count", "3")
    source = _write_source(tmp_path, [scan])
    with pytest.raises(MalformedFileError):
        list(source.iter_spectra())


@pytest.mark.parametrize(
    "contradiction", ["both-roles", "no-compression", "both-precisions"]
)
@pytest.mark.parametrize("conflict_first", [False, True], ids=["last", "first"])
def test_contradictory_numpress_array_metadata_is_refused(
    tmp_path: Path, contradiction: str, conflict_first: bool
) -> None:
    scan = _scan()
    array = scan.find("binaryDataArrayList/binaryDataArray")
    assert array is not None
    accession = {
        "both-roles": _INTENSITY,
        "no-compression": "MS:1000576",
        "both-precisions": "MS:1000521",
    }[contradiction]
    conflict = ET.Element("cvParam", accession=accession)
    array.insert(0 if conflict_first else len(array), conflict)
    source = _write_source(tmp_path, [scan])
    with pytest.raises(MalformedFileError):
        list(source.iter_spectra())


def test_missing_numpress_dependency_fails_through_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _write_source(tmp_path, [_scan()])
    monkeypatch.setitem(sys.modules, "pynumpress", None)
    with pytest.raises(MissingDependencyError, match="pynumpress") as error:
        list(source.iter_spectra())
    assert isinstance(error.value.__cause__, ImportError)


@pytest.mark.parametrize(
    ("vector_name", "failure"),
    [
        ("linear_no_padding", RuntimeError),
        ("pic_half_up_rounding", ValueError),
        ("slof_quantization", OverflowError),
    ],
    ids=["linear-runtime", "pic-value", "slof-overflow"],
)
def test_native_backend_failure_is_typed_and_names_source_scan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    vector_name: str,
    failure: type[Exception],
) -> None:
    source = _write_source(tmp_path, [_scan(vector_name, vector_name)])
    decoder = importlib.import_module("pynumpress")

    def fail(_: bytes) -> None:
        raise failure("qualification backend failure")

    monkeypatch.setattr(decoder, "decode_" + _VECTORS[vector_name]["codec"], fail)
    with pytest.raises(MalformedFileError, match="decoder rejected") as error:
        list(source.iter_spectra())
    assert str(source.path) in str(error.value)
    assert "scan=42" in str(error.value)
    assert isinstance(error.value.__cause__, MalformedFileError)
    assert isinstance(error.value.__cause__.__cause__, failure)


@pytest.mark.parametrize("corruption", ["base64", "unpaired-lengths"])
def test_corrupt_middle_spectrum_stops_without_skipping(
    tmp_path: Path, corruption: str
) -> None:
    middle = _scan(index=1, native_id="scan=broken")
    arrays = middle.find("binaryDataArrayList")
    assert arrays is not None
    if corruption == "base64":
        binary = arrays[1].find("binary")
        assert binary is not None
        binary.text = "!not-base64!"
    else:
        arrays.remove(arrays[1])
        arrays.append(_array(_INTENSITY, "pic_nibble_widths"))
    source = _write_source(tmp_path, [_scan(index=0), middle, _scan(index=2)])
    iterator = source.iter_spectra()
    assert next(iterator).index == 0
    with pytest.raises(MalformedFileError) as error:
        next(iterator)
    assert str(source.path) in str(error.value)
    assert "scan=broken" in str(error.value)
    assert ("base64" if corruption == "base64" else "different lengths") in str(
        error.value
    )
    with pytest.raises(StopIteration):
        next(iterator)


def test_huge_declared_length_is_bookkeeping_not_an_allocation_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _write_source(tmp_path, [_scan(declared=10**100)])

    # Guard Python-side allocation entry points before parsing. Never attempt a
    # real huge allocation; native decoder allocation is a separate work lane.
    def guard(allocator: Callable[..., Any]) -> Callable[..., Any]:
        def bounded(shape: Any, *args: Any, **kwargs: Any) -> Any:
            dimensions = (shape,) if isinstance(shape, int) else tuple(shape)
            assert all(0 <= dimension <= 1024 for dimension in dimensions), (
                "defaultArrayLength was used as an allocation shape",
                dimensions,
            )
            return allocator(shape, *args, **kwargs)

        return bounded

    for name in ("empty", "zeros", "ones", "full"):
        monkeypatch.setattr(np, name, guard(getattr(np, name)))
    spectra = list(source.iter_spectra())
    assert len(spectra) == 1
    assert spectra[0].n_peaks == 6
    assert spectra[0].mz.nbytes == spectra[0].intensity.nbytes == 48
    assert spectra[0].source_warnings == ("declared_array_length_mismatch",)


@pytest.mark.parametrize("compression", ["combined-zlib", "separate-zlib"])
@pytest.mark.parametrize("outer_gzip", [False, True], ids=["xml", "gzip"])
def test_numpress_zlib_source_uses_explicit_policy_and_preserves_pairing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    compression: str,
    outer_gzip: bool,
) -> None:
    policy = SecurityPolicy(allowed_root=tmp_path, max_file_size_bytes=20_000)
    # Any fallback to the process policy (array decode or outer gzip) fails.
    monkeypatch.setattr(
        security, "DEFAULT_POLICY", SecurityPolicy(max_file_size_bytes=1)
    )
    source = _write_source(
        tmp_path,
        [_scan(compression=compression, reverse_arrays=True)],
        outer_gzip=outer_gzip,
        policy=policy,
    )
    assert source.policy is policy
    assert (source.format, source.backend) == ("mzML", "msmcp.mzml")
    spectra = list(source.iter_spectra())
    assert len(spectra) == 1
    _assert_arrays(
        spectra[0],
        _VECTORS["pic_half_up_rounding"]["decoded"],
        _VECTORS["slof_quantization"]["decoded"],
    )


def test_numpress_outer_gzip_is_bounded_by_custom_inflated_file_policy(
    tmp_path: Path,
) -> None:
    scan = _scan(compression="combined-zlib")
    scan.append(ET.Comment("qualification padding " * 1000))
    policy = SecurityPolicy(allowed_root=tmp_path, max_file_size_bytes=4096)
    source = _write_source(tmp_path, [scan], outer_gzip=True, policy=policy)
    assert source.path.stat().st_size < policy.max_file_size_bytes
    with pytest.raises(FileSizeExceededError, match="Decompressed input"):
        list(source.iter_spectra())
