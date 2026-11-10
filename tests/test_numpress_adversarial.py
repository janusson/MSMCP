"""Untrusted Numpress bytes must be checked before native allocation/arithmetic."""

from __future__ import annotations

import base64
import importlib
import struct
import zlib
from typing import Any

import numpy as np
import pytest

from msmcp import mzml
from msmcp.errors import MalformedFileError
from msmcp.security import FileSizeExceededError

_FP = struct.pack(">d", 1.0)


def _decode(raw: bytes, codec: str, *, budget: int = 20_000_000) -> np.ndarray:
    return mzml._decode_binary(
        base64.b64encode(raw).decode(),
        compression=f"numpress-{codec}",
        precision="64",
        max_bytes=budget,
    )


def _forbid_native(monkeypatch: pytest.MonkeyPatch, codec: str) -> None:
    def forbidden(_: bytes) -> None:
        pytest.fail("untrusted payload reached the native decoder")

    monkeypatch.setattr(
        importlib.import_module("pynumpress"), f"decode_{codec}", forbidden
    )


def _pack(nibbles: list[int]) -> bytes:
    if len(nibbles) % 2:
        nibbles = [*nibbles, 0]
    return bytes((nibbles[i] << 4) | nibbles[i + 1] for i in range(0, len(nibbles), 2))


@pytest.mark.parametrize("codec", ["linear", "slof"])
@pytest.mark.parametrize("length", range(8))
def test_truncated_fixed_point_never_reaches_native(
    monkeypatch: pytest.MonkeyPatch, codec: str, length: int
) -> None:
    _forbid_native(monkeypatch, codec)
    with pytest.raises(MalformedFileError):
        _decode(_FP[:length], codec)


@pytest.mark.parametrize("length", [9, 10, 11, 13, 14, 15])
def test_partial_linear_seeds_never_reach_native(
    monkeypatch: pytest.MonkeyPatch, length: int
) -> None:
    _forbid_native(monkeypatch, "linear")
    with pytest.raises(MalformedFileError):
        _decode((_FP + bytes(8))[:length], "linear")


@pytest.mark.parametrize("length", [9, 11, 13, 101])
def test_slof_partial_uint16_never_reaches_native(
    monkeypatch: pytest.MonkeyPatch, length: int
) -> None:
    _forbid_native(monkeypatch, "slof")
    with pytest.raises(MalformedFileError):
        _decode(_FP + bytes(length - 8), "slof")


@pytest.mark.parametrize("codec", ["linear", "pic"])
@pytest.mark.parametrize("head", [*range(7), *range(9, 15)])
@pytest.mark.parametrize("alignment", [0, 1])
def test_truncated_integer_payload_at_each_nibble_width(
    monkeypatch: pytest.MonkeyPatch, codec: str, head: int, alignment: int
) -> None:
    _forbid_native(monkeypatch, codec)
    width = 8 - (head if head <= 8 else head - 8)
    # Truncate by two nibbles so zero padding cannot complete the integer.
    nibbles = [8] * alignment + [head] + [1] * max(0, width - 2)
    raw = (_FP + bytes(8) if codec == "linear" else b"") + _pack(nibbles)
    with pytest.raises(MalformedFileError, match="integer stream"):
        _decode(raw, codec)


@pytest.mark.parametrize("codec", ["linear", "pic"])
def test_padding_is_only_a_final_low_zero_nibble(codec: str) -> None:
    prefix = _FP + bytes(8) if codec == "linear" else b""
    initial = [0.0, 0.0] if codec == "linear" else []
    np.testing.assert_array_equal(_decode(prefix + b"\x80", codec), [*initial, 0.0])
    np.testing.assert_array_equal(
        _decode(prefix + b"\x88", codec), [*initial, 0.0, 0.0]
    )
    for tail in (b"\x81", b"\x00", b"\x80\x00"):
        with pytest.raises(MalformedFileError, match="integer stream"):
            _decode(prefix + tail, codec)


@pytest.mark.parametrize("codec", ["linear", "slof"])
@pytest.mark.parametrize("fp", [0.0, -1.0, float("nan"), float("inf"), -float("inf")])
def test_nonempty_invalid_fixed_points_refused_before_native(
    monkeypatch: pytest.MonkeyPatch, codec: str, fp: float
) -> None:
    _forbid_native(monkeypatch, codec)
    raw = struct.pack(">d", fp) + (bytes(8) if codec == "linear" else bytes(2))
    with pytest.raises(MalformedFileError, match="fixed-point"):
        _decode(raw, codec)


@pytest.mark.parametrize("codec", ["linear", "slof"])
def test_empty_header_does_not_interpret_unused_fixed_point(
    monkeypatch: pytest.MonkeyPatch, codec: str
) -> None:
    _forbid_native(monkeypatch, codec)
    actual = _decode(bytes(8), codec)
    assert actual.shape == (0,)
    assert not actual.flags.writeable


@pytest.mark.parametrize("codec", ["linear", "slof"])
def test_finite_positive_fixed_point_can_still_overflow_output(codec: str) -> None:
    raw = struct.pack(">d", 5e-324) + (
        struct.pack("<I", 1) if codec == "linear" else b"\xff\xff"
    )
    with pytest.raises(MalformedFileError, match="invalid values"):
        _decode(raw, codec)


@pytest.mark.parametrize("negative", [False, True])
def test_linear_signed64_overflow_refused_before_native(
    monkeypatch: pytest.MonkeyPatch, negative: bool
) -> None:
    _forbid_native(monkeypatch, "linear")
    # Two zero seeds, then constant +/- int32 residuals. At residual 92,682,
    # the signed-64 predictor/result overflows in pynumpress 0.1.5's C++.
    residual = [0, *([0] * 7), 8] if negative else [0, *([15] * 7), 7]
    raw = _FP + bytes(8) + _pack(residual * 92_682)
    with pytest.raises(MalformedFileError, match="64-bit"):
        _decode(raw, "linear")


@pytest.mark.parametrize("negative", [False, True])
def test_linear_near_signed64_boundary_is_not_rejected(negative: bool) -> None:
    residual = [0, *([0] * 7), 8] if negative else [0, *([15] * 7), 7]
    raw = _FP + bytes(8) + _pack(residual * 92_681)
    # Preflight only: the adjacent unsafe case is never called through native.
    assert mzml._validate_numpress(raw, codec="linear", max_bytes=20_000_000) == 92_683


@pytest.mark.parametrize("codec", ["linear", "pic", "slof"])
def test_native_reservation_bound_exact_threshold(
    monkeypatch: pytest.MonkeyPatch, codec: str
) -> None:
    raw = {"linear": _FP + bytes(8), "pic": b"\x88", "slof": _FP + bytes(2)}[codec]
    factor = 4 if codec == "slof" else 16
    actual = _decode(raw, codec, budget=len(raw) * factor)
    assert actual.size == (1 if codec == "slof" else 2)
    _forbid_native(monkeypatch, codec)
    with pytest.raises(FileSizeExceededError):
        _decode(raw, codec, budget=len(raw) * factor - 1)


def test_reject_native_reservation_before_scanning_integer_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> int:
        pytest.fail("oversized native reservation was scanned before rejection")

    monkeypatch.setattr(mzml, "_numpress_integer_count", forbidden)
    with pytest.raises(FileSizeExceededError):
        _decode(b"\x88" * 100, "pic", budget=100)


def test_oversized_encoded_input_is_rejected_before_base64_allocation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    text = base64.b64encode(b"\x88" * 128).decode()

    def forbidden(*args: Any, **kwargs: Any) -> bytes:
        pytest.fail("oversized input reached base64 allocation")

    monkeypatch.setattr(base64, "b64decode", forbidden)
    with pytest.raises(FileSizeExceededError):
        mzml._decode_binary(
            text, compression="numpress-pic", precision="64", max_bytes=32
        )


@pytest.mark.parametrize("codec", ["linear", "pic", "slof"])
@pytest.mark.parametrize(
    "corruption", ["truncated", "checksum", "garbage", "trailing", "second-stream"]
)
def test_corrupted_zlib_is_not_partially_accepted(
    monkeypatch: pytest.MonkeyPatch, codec: str, corruption: str
) -> None:
    _forbid_native(monkeypatch, codec)
    raw = {"linear": _FP + bytes(8), "pic": b"\x88", "slof": _FP + bytes(2)}[codec]
    compressed = zlib.compress(raw)
    payload = {
        "truncated": compressed[:-1],
        "checksum": compressed[:-1] + bytes([compressed[-1] ^ 1]),
        "garbage": b"not zlib",
        "trailing": compressed + b"garbage",
        "second-stream": compressed + zlib.compress(raw),
    }[corruption]
    with pytest.raises(MalformedFileError, match="zlib"):
        _decode(payload, codec + "-zlib")


def test_zlib_output_limit_precedes_native_allocation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _forbid_native(monkeypatch, "pic")
    with pytest.raises(FileSizeExceededError):
        _decode(zlib.compress(b"\x88" * 100_000), "pic-zlib", budget=1000)


@pytest.mark.parametrize("codec", ["linear", "pic", "slof"])
@pytest.mark.parametrize(
    "invalid", ["nan", "inf", "wrong-count", "wrong-shape", "not-numeric"]
)
def test_native_result_contract_is_checked(
    monkeypatch: pytest.MonkeyPatch, codec: str, invalid: str
) -> None:
    raw = {"linear": _FP + bytes(8), "pic": b"\x88", "slof": _FP + bytes(4)}[codec]
    result: Any = {
        "nan": [0.0, float("nan")],
        "inf": [0.0, float("inf")],
        "wrong-count": [0.0],
        "wrong-shape": [[0.0, 0.0]],
        "not-numeric": ["not-a-number", "no"],
    }[invalid]

    def decode(_: bytes) -> Any:
        return result

    monkeypatch.setattr(
        importlib.import_module("pynumpress"), f"decode_{codec}", decode
    )
    with pytest.raises(MalformedFileError):
        _decode(raw, codec)
