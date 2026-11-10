"""Independent original-reference Numpress answers, not pynumpress round trips.

See docs/numpress-qualification.md for pinned sources, regeneration, tolerances,
and native safety findings. The normal test run is offline and needs no compiler.
"""

from __future__ import annotations

import base64
import json
import math
import xml.etree.ElementTree as ET
import zlib
from pathlib import Path
from typing import Literal, TypedDict, cast

import numpy as np
import pytest

from msmcp.mzml import _decode_binary, _parse_spectrum


class KnownAnswer(TypedDict):
    name: str
    codec: Literal["linear", "pic", "slof"]
    fixed_point: float
    input: list[float]
    hex: str
    decoded: list[float]


class ReferenceFixture(TypedDict):
    reference_revision: str
    source_sha256: dict[str, str]
    vectors: list[KnownAnswer]


_DATA = Path(__file__).parent / "data"
_FIXTURE = cast(
    ReferenceFixture,
    json.loads((_DATA / "numpress_known_answers.json").read_text()),
)
_VECTORS = _FIXTURE["vectors"]
_PARSER_VECTORS = [
    vector
    for vector in _VECTORS
    if vector["name"]
    in {
        "linear_quantization_signed_residuals",
        "pic_nibble_widths",
        "slof_uint16_byte_order",
    }
]
# PSI-MS CV d0f4eca3ed40e220858ea2c4d7c03215b38e48ef. The combined
# terms explicitly name Numpress *followed by* zlib; decode in reverse order.
_ACCESSIONS = {
    "linear": ("MS:1002312", "MS:1002746"),
    "pic": ("MS:1002313", "MS:1002747"),
    "slof": ("MS:1002314", "MS:1002748"),
}


def test_reference_fixture_is_pinned() -> None:
    assert _FIXTURE["reference_revision"] == "61acddbce26db586d1cf4e873f2acf225c9d8526"
    assert _FIXTURE["source_sha256"] == {
        "src/main/cpp/MSNumpress.hpp": (
            "399edb6f33c4f950def439d12dae5eef0d883e05335d38cddf8e3072c012a7e8"
        ),
        "src/main/cpp/MSNumpress.cpp": (
            "55fdaac456ee9131bc0921918d71564937fc83e79d068dc53e493a08e12fe89f"
        ),
    }
    assert {vector["codec"] for vector in _VECTORS} == {"linear", "pic", "slof"}
    assert len({vector["name"] for vector in _VECTORS}) == len(_VECTORS) == 17


@pytest.mark.parametrize("vector", _VECTORS, ids=lambda vector: vector["name"])
@pytest.mark.parametrize("wrapped", [False, True], ids=["raw", "zlib"])
@pytest.mark.parametrize("precision", ["32", "64"])
def test_decode_original_reference_bytes(
    vector: KnownAnswer, wrapped: bool, precision: str
) -> None:
    raw = bytes.fromhex(vector["hex"])
    payload = zlib.compress(raw) if wrapped else raw
    compression = f"numpress-{vector['codec']}" + ("-zlib" if wrapped else "")
    actual = _decode_binary(
        base64.b64encode(payload).decode("ascii"),
        compression=compression,
        precision=precision,
    )
    expected = np.asarray(vector["decoded"], dtype=np.float64)
    # Compare decoded binary64 values, separately from the lossy source bound.
    # Four ULP accommodates platform libm exp variation for SLOF, not arbitrary
    # relative error that could hide a wrong fixed point, seed, or residual.
    np.testing.assert_array_max_ulp(actual, expected, maxulp=4)
    assert actual.dtype == np.dtype("float64")
    assert not actual.flags.writeable


@pytest.mark.parametrize("vector", _VECTORS, ids=lambda vector: vector["name"])
def test_reference_source_quantization_bound(vector: KnownAnswer) -> None:
    """Keep encoding error distinct from decoder implementation agreement."""
    original = np.asarray(vector["input"], dtype=np.float64)
    decoded = np.asarray(vector["decoded"], dtype=np.float64)
    fp = vector["fixed_point"]
    if vector["codec"] == "linear":
        bound = np.full(original.shape, 0.5 / fp)
    elif vector["codec"] == "pic":
        bound = np.full(original.shape, 0.5)
    else:
        # |log(decoded+1)-log(original+1)| <= 0.5/fp. This is a
        # multiplicative bound on value+1, not on value (which may be zero).
        bound = (original + 1.0) * math.expm1(0.5 / fp)
    rounding_slack = 4 * np.abs(np.spacing(np.maximum(original, decoded)))
    assert np.all(np.abs(decoded - original) <= bound + rounding_slack)


@pytest.mark.parametrize("vector", _PARSER_VECTORS, ids=lambda vector: vector["codec"])
@pytest.mark.parametrize("compression", ["plain", "combined-zlib", "separate-zlib"])
@pytest.mark.parametrize("reverse_terms", [False, True], ids=["forward", "reverse"])
@pytest.mark.parametrize("precision_term", ["MS:1000521", "MS:1000523"])
def test_parser_original_reference_arrays(
    vector: KnownAnswer,
    compression: str,
    reverse_terms: bool,
    precision_term: str,
) -> None:
    """All codecs, zlib term spellings, CV orders and float precision tags.

    The old Numpress term plus MS:1000574 is an interoperability spelling,
    not a claim that the CV itself specifies compression order for two terms.
    The combined terms are authoritative about that order.
    """
    plain, combined = _ACCESSIONS[vector["codec"]]
    terms = [precision_term, combined if compression == "combined-zlib" else plain]
    if compression == "separate-zlib":
        terms.append("MS:1000574")
    if reverse_terms:
        terms.reverse()
    raw = bytes.fromhex(vector["hex"])
    if compression != "plain":
        raw = zlib.compress(raw)
    payload = base64.b64encode(raw).decode("ascii")
    count = len(vector["decoded"])
    root = ET.Element("{http://psi.hupo.org/ms/mzml}spectrum")
    root.set("index", "0")
    root.set("defaultArrayLength", str(count))
    arrays = ET.SubElement(root, "binaryDataArrayList", count="2")
    for kind in ("MS:1000514", "MS:1000515"):
        array = ET.SubElement(arrays, "binaryDataArray")
        for accession in [kind, *terms]:
            ET.SubElement(array, "cvParam", accession=accession)
        # XML/base64 whitespace must not change the Numpress bytes.
        ET.SubElement(array, "binary").text = f"\n  {payload[:12]}\n {payload[12:]}\n"
    spectrum = _parse_spectrum(root)
    expected = np.asarray(vector["decoded"], dtype=np.float64)
    for actual in (spectrum.mz, spectrum.intensity):
        assert actual.shape == expected.shape
        np.testing.assert_array_max_ulp(actual, expected, maxulp=4)
        assert not actual.flags.writeable
    assert not spectrum.source_warnings
