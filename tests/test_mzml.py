"""Tests for the mzML reader, I/O tool, and error taxonomy."""

from __future__ import annotations

import base64
import importlib
import xml.etree.ElementTree as ET
import zlib
from collections.abc import Callable
from pathlib import Path

import numpy as np
import pytest

from msmcp.errors import (
    InaccessiblePathError,
    MalformedFileError,
    MissingDependencyError,
    UnsupportedFormatError,
)
from msmcp.mzml import (
    _decode_binary,
    _parse_spectrum,
    declared_spectrum_count,
    iter_spectra,
    resolve_mzml_path,
)
from msmcp.security import FileSizeExceededError, SecurityPolicy
from msmcp.tools import io


# ---------------------------------------------------------------------------
# Parser: binary decoding
# ---------------------------------------------------------------------------
class TestDecodeBinary:
    def test_plain_float64(self) -> None:
        arr = np.array([1.0, 2.5, -3.0], dtype="<f8")
        payload = base64.b64encode(arr.tobytes()).decode()
        assert _decode_binary(
            payload, compression="none", precision="64"
        ) == pytest.approx(arr)

    def test_zlib_float64(self) -> None:
        arr = np.array([1.0, 2.5, -3.0], dtype="<f8")
        payload = base64.b64encode(zlib.compress(arr.tobytes())).decode()
        assert _decode_binary(
            payload, compression="zlib", precision="64"
        ) == pytest.approx(arr)

    def test_zlib_array_beyond_the_byte_budget_is_refused(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A zlib array inside a plain mzML is bounded by its *inflated* size.

        A ``.mzML`` is not gzip, so ``security.bounded_stream`` never sees this
        payload; the parser must bound it, or a tiny file could carry an array
        that inflates far past ``max_file_size_bytes``.
        """
        from msmcp import security

        monkeypatch.setattr(
            security,
            "DEFAULT_POLICY",
            SecurityPolicy(max_file_size_bytes=4096),
        )
        # 4 MiB of zeros compresses to a few KB but inflates past the budget.
        payload = base64.b64encode(zlib.compress(b"\x00" * (4 << 20))).decode()
        with pytest.raises(FileSizeExceededError, match="inflates beyond"):
            _decode_binary(payload, compression="zlib", precision="64")

    def test_float32(self) -> None:
        arr32 = np.array([1.0, -2.0, 3.5], dtype="<f4")
        payload = base64.b64encode(arr32.tobytes()).decode()
        assert _decode_binary(
            payload, compression="none", precision="32"
        ) == pytest.approx(arr32.astype("f8"))

    def test_bad_base64_is_malformed(self) -> None:
        with pytest.raises(MalformedFileError, match="base64"):
            _decode_binary("!!!not-base64!!!", compression="none", precision="64")

    def test_truncated_zlib_is_malformed(self) -> None:
        """A truncated zlib stream stays a MalformedFileError, not a partial read.

        The bounded decompressor replaces ``zlib.decompress``; it must preserve
        that a stream missing its end marker is refused, not silently returned
        with the bytes it managed to inflate.
        """
        arr = np.array([1.0, 2.0, 3.0], dtype="<f8")
        blob = zlib.compress(arr.tobytes())
        payload = base64.b64encode(blob[: len(blob) // 2]).decode()
        with pytest.raises(MalformedFileError, match="zlib"):
            _decode_binary(payload, compression="zlib", precision="64")

    def test_numpress_missing_dependency(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import msmcp.mzml as mzml

        def no_numpress(name: str) -> object:
            if name == "pynumpress":
                raise ImportError("not installed")
            return __import__(name)

        monkeypatch.setattr(mzml.importlib, "import_module", no_numpress)
        with pytest.raises(MissingDependencyError, match="pynumpress"):
            _decode_binary("", compression="numpress", precision="64")

    @pytest.mark.parametrize(
        ("codec", "values", "tolerance"),
        [
            ("linear", np.array([100.0, 101.0, 102.0, 103.0]), 1e-5),
            ("pic", np.array([0.0, 1.0, 256.0, 3.0]), 0.0),
            ("slof", np.array([0.0, 1.0, 10.0, 100.0]), 0.02),
        ],
    )
    def test_numpress_variants_decode_read_only(
        self, codec: str, values: np.ndarray, tolerance: float
    ) -> None:
        pynumpress = importlib.import_module("pynumpress")

        if codec == "linear":
            encoded = pynumpress.encode_linear(values, 1000.0)
        elif codec == "pic":
            encoded = pynumpress.encode_pic(values)
        else:
            encoded = pynumpress.encode_slof(values, 1000.0)
        actual = _decode_binary(
            base64.b64encode(encoded).decode(),
            compression=f"numpress-{codec}",
            precision="64",
        )
        assert actual == pytest.approx(values, abs=tolerance)
        assert not actual.flags.writeable

    def test_numpress_linear_single_value(self) -> None:
        pynumpress = importlib.import_module("pynumpress")
        values = np.array([123.456])
        encoded = pynumpress.encode_linear(values, 1000.0)
        actual = _decode_binary(
            base64.b64encode(encoded).decode(),
            compression="numpress-linear",
            precision="64",
        )
        assert actual == pytest.approx(values, abs=0.001)

    @pytest.mark.parametrize(
        ("codec", "payload"),
        [
            ("linear", b"\x00" * 15),
            ("pic", b"\x00"),
            ("slof", b"\x00" * 9),
        ],
    )
    def test_numpress_malformed_payload_is_rejected_before_decode(
        self, codec: str, payload: bytes
    ) -> None:
        with pytest.raises(MalformedFileError, match="Numpress"):
            _decode_binary(
                base64.b64encode(payload).decode(),
                compression=f"numpress-{codec}",
                precision="64",
            )

    def test_upstream_numpress_fixture_decodes_real_linear_and_pic_arrays(self) -> None:
        fixture = Path(__file__).parent / "data" / "mini_numpress.chrom.mzML"
        root = ET.parse(fixture).getroot()
        arrays = [node for node in root.iter() if node.tag.endswith("binaryDataArray")]
        decoded: dict[str, np.ndarray] = {}
        for array in arrays:
            cv = next(
                child
                for child in array
                if child.tag.endswith("cvParam")
                and child.get("accession") in {"MS:1002312", "MS:1002313"}
            )
            codec = "linear" if cv.get("accession") == "MS:1002312" else "pic"
            payload = next(
                child for child in array if child.tag.endswith("binary")
            ).text
            decoded[codec] = _decode_binary(
                payload or "",
                compression=f"numpress-{codec}",
                precision="64",
            )
        assert decoded["linear"][:3] == pytest.approx(
            [2302.5300000107377, 2305.939999978524, 2309.3600001288546]
        )
        assert decoded["linear"][-1] == pytest.approx(2899.960000343612)
        assert decoded["pic"][:5] == pytest.approx(np.zeros(5))
        assert decoded["pic"][-1] == pytest.approx(28.0)

    def test_numpress_zlib_combination(self) -> None:
        pynumpress = importlib.import_module("pynumpress")

        values = np.array([0.0, 2.0, 5.0, 100.0])
        encoded = pynumpress.encode_pic(values)
        payload = base64.b64encode(zlib.compress(encoded)).decode()
        actual = _decode_binary(
            payload,
            compression="numpress-pic-zlib",
            precision="64",
        )
        assert actual == pytest.approx(values)

    def test_numpress_native_decoder_reservation_obeys_byte_budget(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pynumpress = importlib.import_module("pynumpress")
        from msmcp import security

        payload = base64.b64encode(
            pynumpress.encode_pic(np.array([0.0, 1.0, 2.0, 3.0]))
        ).decode()
        monkeypatch.setattr(
            security, "DEFAULT_POLICY", SecurityPolicy(max_file_size_bytes=16)
        )
        with pytest.raises(FileSizeExceededError, match="byte limit"):
            _decode_binary(
                payload,
                compression="numpress-pic",
                precision="64",
            )

    def test_spectrum_parser_uses_numpress_array_cv_terms(self) -> None:
        pynumpress = importlib.import_module("pynumpress")

        mz = np.array([100.0, 101.0, 102.0, 103.0])
        intensity = np.array([0.0, 1.0, 10.0, 100.0])
        mz_payload = base64.b64encode(pynumpress.encode_linear(mz, 1000.0)).decode()
        intensity_payload = base64.b64encode(
            pynumpress.encode_slof(intensity, 1000.0)
        ).decode()
        xml = f"""<spectrum index="0" defaultArrayLength="4">
          <cvParam accession="MS:1000511" value="1"/>
          <binaryDataArrayList count="2">
            <binaryDataArray><cvParam accession="MS:1000514"/>
              <cvParam accession="MS:1002312"/><binary>{mz_payload}</binary>
            </binaryDataArray>
            <binaryDataArray><cvParam accession="MS:1000515"/>
              <cvParam accession="MS:1002314"/><binary>{intensity_payload}</binary>
            </binaryDataArray>
          </binaryDataArrayList>
        </spectrum>"""
        spectrum = _parse_spectrum(ET.fromstring(xml))
        assert spectrum.mz == pytest.approx(mz)
        assert spectrum.intensity == pytest.approx(intensity, abs=0.02)

    def test_numpress_declared_length_mismatch_is_reported_not_refused(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        pynumpress = importlib.import_module("pynumpress")
        values = np.array([100.0, 101.0, 102.0, 103.0])
        mz_payload = base64.b64encode(pynumpress.encode_linear(values, 1000.0)).decode()
        intensity_payload = base64.b64encode(pynumpress.encode_pic(values)).decode()
        xml = f"""<mzML><run><spectrumList count="1">
          <spectrum index="0" defaultArrayLength="5">
            <cvParam accession="MS:1000511" value="1"/>
            <binaryDataArrayList count="2">
              <binaryDataArray><cvParam accession="MS:1000514"/>
                <cvParam accession="MS:1002312"/><binary>{mz_payload}</binary>
              </binaryDataArray>
              <binaryDataArray><cvParam accession="MS:1000515"/>
                <cvParam accession="MS:1002313"/><binary>{intensity_payload}</binary>
              </binaryDataArray>
            </binaryDataArrayList>
          </spectrum>
        </spectrumList></run></mzML>"""
        path = tmp_path / "numpress_length_mismatch.mzML"
        path.write_text(xml)

        with caplog.at_level("WARNING", logger="msmcp.mzml"):
            spectra = list(iter_spectra(path))

        assert len(spectra) == 1
        assert spectra[0].mz.size == 4
        assert spectra[0].intensity.size == 4
        assert "declare a peak count" in caplog.text


# ---------------------------------------------------------------------------
# Parser: spectrum iteration and counts
# ---------------------------------------------------------------------------
class TestMzMLParser:
    def test_declared_spectrum_count(self, valid_mzml: Path) -> None:
        assert declared_spectrum_count(valid_mzml) == 3

    def test_spectrum_tic_and_counts(self, valid_mzml: Path) -> None:
        spectra = list(iter_spectra(valid_mzml))
        assert len(spectra) == 3
        assert [s.tic for s in spectra] == pytest.approx([60.0, 10.0, 10.0])
        assert [s.n_peaks for s in spectra] == [3, 2, 4]

    def test_spectrum_metadata(self, valid_mzml: Path) -> None:
        first = next(iter_spectra(valid_mzml))
        assert first.index == 0
        assert first.ms_level == 1
        assert first.precursor_mz == pytest.approx(300.0)
        assert first.retention_time == pytest.approx(0.0)

    def test_truncated_file_is_malformed(self, truncated_mzml: Path) -> None:
        with pytest.raises(MalformedFileError):
            list(iter_spectra(truncated_mzml))

    def test_polarity_is_parsed_when_the_file_states_it(
        self, mzml_with_polarity: Path
    ) -> None:
        spectra = list(iter_spectra(mzml_with_polarity))
        assert [s.polarity for s in spectra] == ["positive", "negative"]

    def test_polarity_is_none_when_the_file_omits_it(self, valid_mzml: Path) -> None:
        # No polarity cvParam → None, never a guessed default.
        assert all(s.polarity is None for s in iter_spectra(valid_mzml))


# ---------------------------------------------------------------------------
# Error taxonomy at the access boundary
# ---------------------------------------------------------------------------
class TestErrorTaxonomy:
    @pytest.mark.parametrize("ext", [".mgf", ".raw", ".d", ".txt"])
    def test_unsupported_formats(self, tmp_path: Path, ext: str) -> None:
        path = tmp_path / f"data{ext}"
        path.write_text("x")
        policy = SecurityPolicy(allowed_root=tmp_path)
        with pytest.raises(UnsupportedFormatError):
            resolve_mzml_path(path, policy)


# ---------------------------------------------------------------------------
# Tool-level behaviour
# ---------------------------------------------------------------------------
class TestLoadMzMLSummary:
    def test_summary(
        self,
        io_tools: dict[str, Callable[..., str]],
        valid_mzml: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            io, "DEFAULT_POLICY", SecurityPolicy(allowed_root=valid_mzml.parent)
        )
        out = io_tools["load_mzml_summary"](str(valid_mzml))
        assert "valid.mzML" in out
        assert "Summarised 3 of 3 total spectra." in out
        assert "TIC:" in out

    def test_summary_respects_max_spectra(
        self,
        io_tools: dict[str, Callable[..., str]],
        valid_mzml: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            io, "DEFAULT_POLICY", SecurityPolicy(allowed_root=valid_mzml.parent)
        )
        out = io_tools["load_mzml_summary"](str(valid_mzml), max_spectra=2)
        assert "Summarised 2 of 3 total spectra." in out

    def test_missing_file_raises(
        self,
        io_tools: dict[str, Callable[..., str]],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(io, "DEFAULT_POLICY", SecurityPolicy(allowed_root=tmp_path))
        with pytest.raises(InaccessiblePathError):
            io_tools["load_mzml_summary"](str(tmp_path / "missing.mzML"))

    def test_truncated_file_raises(
        self,
        io_tools: dict[str, Callable[..., str]],
        truncated_mzml: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            io, "DEFAULT_POLICY", SecurityPolicy(allowed_root=truncated_mzml.parent)
        )
        with pytest.raises(MalformedFileError):
            io_tools["load_mzml_summary"](str(truncated_mzml))


# ---------------------------------------------------------------------------
# Zero-length arrays: real acquisitions contain legally empty scans
# ---------------------------------------------------------------------------
class TestEmptyBinaryArrays:
    """An empty array is data (no peaks), not a missing payload.

    ``COE001_16ppm_5uL.mzML`` (Bruker maXis QTOF) carries 4,990 zero-length
    arrays across 2,495 of its 9,286 spectra, written as ``encodedLength="0"``
    with a self-closing ``<binary/>``.  The reader treated every one of them as
    corruption and aborted the whole file at its third spectrum, so a real
    acquisition could not be read at all.
    """

    def test_zero_length_array_yields_an_empty_spectrum(
        self, mzml_with_empty_scan: Path
    ) -> None:
        """An empty scan parses, and does not stop the spectra after it."""
        spectra = list(iter_spectra(mzml_with_empty_scan))

        assert len(spectra) == 3
        empty = spectra[1]
        assert empty.ms_level == 2
        assert empty.mz.size == 0
        assert empty.intensity.size == 0
        assert empty.precursor_mz == pytest.approx(89.50771332)
        # The reader keeps going: the scan after the empty one still arrives.
        assert spectra[2].mz.size == 2
        assert spectra[2].precursor_mz == pytest.approx(200.0)

    def test_empty_arrays_are_read_only(self, mzml_with_empty_scan: Path) -> None:
        """Empty arrays share the immutability of every other decoded array."""
        empty = list(iter_spectra(mzml_with_empty_scan))[1]
        assert not empty.mz.flags.writeable
        assert not empty.intensity.flags.writeable

    def test_absent_binary_element_is_an_empty_array(
        self, mzml_with_absent_binary: Path
    ) -> None:
        """``<binary>`` has ``minOccurs="0"``: omitting it still means no peaks."""
        spectra = list(iter_spectra(mzml_with_absent_binary))
        assert spectra[0].mz.size == 0
        assert spectra[0].intensity.size == 0

    def test_declared_peaks_with_an_empty_payload_is_malformed(
        self, mzml_declares_peaks_with_empty_payload: Path
    ) -> None:
        """Emptiness is legal; a declaration that contradicts it is not.

        Strictness is kept where it earns its keep: a file that says it has
        three peaks and carries none is corruption, exactly as a ``Num Peaks``
        header that disagrees with the peak lines is in the MSP reader.
        """
        with pytest.raises(MalformedFileError, match="declares"):
            list(iter_spectra(mzml_declares_peaks_with_empty_payload))


# ---------------------------------------------------------------------------
# A declaration the arrays cannot supply: reported, not refused
# ---------------------------------------------------------------------------
class TestDeclaredLengthMismatch:
    """A declaration the arrays cannot supply is reported, not refused.

    Emptiness in a spectrum that declares peaks is unambiguous corruption and
    still raises.  A payload that decodes to *fewer* values than the spectrum
    claims is a different case: it may be a writer's bookkeeping rather than
    corruption, and refusing the file would make a released server reject data it
    accepts today.  It is reported once per file instead — visible without the
    breakage — and the decoded arrays, which are what every downstream tool
    actually uses, are yielded as they are.
    """

    def test_a_short_payload_is_read_and_reported(
        self,
        mzml_declares_more_than_it_supplies: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The file is read; the inconsistency is named once, at WARNING."""
        with caplog.at_level("WARNING", logger="msmcp.mzml"):
            spectra = list(iter_spectra(mzml_declares_more_than_it_supplies))

        assert len(spectra) == 1
        assert spectra[0].mz.size == 2  # read as decoded, not padded or refused
        assert spectra[0].intensity.size == 2

        assert "declare a peak count" in caplog.text
        assert "1 spectrum" in caplog.text

    def test_a_consistent_file_is_not_reported(
        self, valid_mzml: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """No warning when the declaration and the arrays agree."""
        with caplog.at_level("WARNING", logger="msmcp.mzml"):
            list(iter_spectra(valid_mzml))

        assert caplog.text == ""

    def test_the_report_is_one_line_per_file_not_per_spectrum(
        self,
        mzml_two_short_payloads: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Aggregated on purpose: a systematically off writer must not spam."""
        with caplog.at_level("WARNING", logger="msmcp.mzml"):
            list(iter_spectra(mzml_two_short_payloads))

        warnings = [r for r in caplog.records if r.levelno >= 30]
        assert len(warnings) == 1
        assert "2 spectrum" in warnings[0].getMessage()
