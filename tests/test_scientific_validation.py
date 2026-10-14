"""Independent scientific validation for MSMCP.

This module is deliberately different in kind from the rest of the suite.  The
tests in ``test_chem.py`` and ``test_mzml.py`` are *software* tests: their
expected values were written alongside the implementation they check, so they
guard against regressions but cannot show the implementation is scientifically
right.  The tests here check MSMCP against evidence it did not generate:

* **Analytical reference calculations** - an isotope mass/abundance table and
  exact multinomial expansion transcribed independently of
  ``msmcp.tools.chem`` (see ``drafts/independent_reference.py`` for the
  derivation), and the closed-form binomial for a carbon-only envelope.
* **A standards-conformant mzML document** built by this file's own writer, not
  by ``tests/conftest.py``'s fixtures, so ingestion is checked against a
  producer MSMCP does not own.
* **A real MCP session** over the stdio transport - the same JSON-RPC a host
  speaks - exercising ``load_spectrum`` -> ``summarise_reference`` ->
  ``annotate_isotopes`` -> ``release_reference``.

Reference values, their provenance and the justification for each tolerance are
recorded in ``SCIENTIFIC_VALIDATION.md``.  Nothing in this module imports
MSMCP's isotope table or fixture builders to produce an expected value.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import sys
import threading
import time
import zlib
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from msmcp.mzml import iter_spectra
from msmcp.tools.chem import _isotope_pattern, _parse_formula

# ---------------------------------------------------------------------------
# 1. Exact mass and isotope envelope - independent analytical reference
# ---------------------------------------------------------------------------
# Reference values re-derived (drafts/independent_reference.py) from an
# independent transcription of AME2020 atomic masses and CIAAW representative
# abundances, expanded with the *exact* multinomial formula.  They are not
# produced by _isotope_pattern.
#
#   formula: (monoisotopic mass / Da, M+1 / M, M+2 / M)
#
# Tolerances:
#   * monoisotopic mass  abs 1.0e-4 Da  (0.1 mDa) - well above the <1e-6 Da
#     disagreement observed, and far below unit resolution.
#   * M+1 / M+2          abs 2.0e-3     - set by the ~0.1 % spread in the
#     published ³⁷Cl/³⁵Cl ratio between CIAAW editions (0.2422 vs 0.2424), which
#     alone moves a halogen-bearing M+2 by ~1e-3.  The *algorithm* agrees with
#     the exact expansion to machine precision when both use the same table
#     (asserted below), so this tolerance measures table choice, not a bug.
_REFERENCE_ISOTOPES: dict[str, tuple[float, float, float]] = {
    "CH4": (16.031300, 0.011276, 0.000005),
    "H2O": (18.010565, 0.000611, 0.002055),
    "C2H6O": (46.041865, 0.022702, 0.002196),
    "C6H12O6": (180.063388, 0.068560, 0.014329),
    "C7H6O": (106.041865, 0.076781, 0.004593),
    "C8H10N4O2": (194.080376, 0.103212, 0.008941),
    "CHCl3": (117.914383, 0.010931, 0.959875),
    "C6H5Cl": (112.007978, 0.065469, 0.321750),
    "Cl2": (69.937705, 0.000000, 0.639916),
    "Br2": (157.836675, 0.000000, 1.945551),
}

_MONO_ABS_DA = 1.0e-4
_ABUNDANCE_ABS = 2.0e-3


class TestIsotopeReferenceValues:
    """Monoisotopic masses and M+1/M+2 envelopes against an independent table."""

    @pytest.mark.parametrize("formula", sorted(_REFERENCE_ISOTOPES))
    def test_monoisotopic_mass_matches_independent_table(self, formula: str) -> None:
        expected = _REFERENCE_ISOTOPES[formula][0]
        pattern = _isotope_pattern(_parse_formula(formula))
        assert pattern[0][0] == pytest.approx(expected, abs=_MONO_ABS_DA)

    @pytest.mark.parametrize("formula", sorted(_REFERENCE_ISOTOPES))
    def test_m1_m2_abundances_match_independent_table(self, formula: str) -> None:
        _mono, m1, m2 = _REFERENCE_ISOTOPES[formula]
        pattern = _isotope_pattern(_parse_formula(formula))
        assert pattern[0][1] == pytest.approx(1.0, abs=1e-12)
        assert pattern[1][1] == pytest.approx(m1, abs=_ABUNDANCE_ABS)
        assert pattern[2][1] == pytest.approx(m2, abs=_ABUNDANCE_ABS)

    def test_carbon_envelope_is_the_binomial_closed_form(self) -> None:
        """A carbon-only M+1 is n·p/(1-p) exactly - a genuinely independent value.

        With only ¹³C (p = 0.0107) able to shift carbon by +1, the exact result
        is ``C(n, 1)·p/(1-p)``; the implementation must reproduce it to
        floating-point, not merely approximately.
        """
        p13 = 0.0107
        for n in (1, 6, 8, 20):
            formula = f"C{n}"
            expected = n * p13 / (1.0 - p13)
            pattern = _isotope_pattern(_parse_formula(formula))
            assert pattern[1][1] == pytest.approx(expected, rel=1e-12)

    def test_carbon_m2_is_the_binomial_closed_form(self) -> None:
        """M+2 for n carbons is ``C(n, 2)·(p/(1-p))**2``."""
        p13 = 0.0107
        ratio = p13 / (1.0 - p13)
        for n in (6, 8, 20):
            expected = (n * (n - 1) / 2) * ratio**2
            pattern = _isotope_pattern(_parse_formula(f"C{n}"))
            assert pattern[2][1] == pytest.approx(expected, rel=1e-12)

    def test_the_13c_substitution_shift_is_one_neutron_short_of_the_neutron(
        self,
    ) -> None:
        """M+1 shifts by the isotope-substitution difference, not the neutron mass.

        This is the exact-mass distinction the earlier implementation got wrong.
        For a carbon-only formula the shift is exactly ¹³C - ¹²C = 1.0033548 Da,
        four orders of magnitude from the neutron mass (1.0086649 Da); it is
        checkable with no reference table at all.
        """
        from msmcp.tools.chem import NEUTRON_MASS

        # Pure-carbon: the shift must be the ¹³C-¹²C difference, bit-for-bit.
        c8 = _isotope_pattern(_parse_formula("C8"))
        # ¹³C differs between printed tables at the ~3e-9 Da level, so this is
        # checked to 1 µDa - still orders of magnitude tighter than the 5.3 mDa
        # neutron-mass error it exists to catch.
        assert (c8[1][0] - c8[0][0]) == pytest.approx(1.0033548378, abs=1e-6)

        # A heteroatom formula is an abundance-weighted mix, but every +1
        # substitution lies in the physically possible band and none of them is
        # the neutron mass.
        caffeine = _isotope_pattern(_parse_formula("C8H10N4O2"))
        shift = caffeine[1][0] - caffeine[0][0]
        assert 0.9970 < shift < 1.0063  # from 15N-14N to 2H-1H
        assert abs(shift - NEUTRON_MASS) > 2.0e-3

    def test_halogens_contribute_to_m2_not_m1(self) -> None:
        """Cl and Br have no +1 isotope; their signal is at M+2 by construction."""
        for formula in ("Cl", "Br"):
            pattern = _isotope_pattern(_parse_formula(formula))
            assert pattern[1][1] == pytest.approx(0.0, abs=1e-12)
            assert pattern[2][1] > 0.3  # the secondary isotope carries M+2

        # A carbon/hydrogen-bearing chloride still has a (small) M+1 - but it
        # comes from ¹³C/²H, not from chlorine.
        chloroform = _isotope_pattern(_parse_formula("CHCl3"))
        assert chloroform[1][1] == pytest.approx(0.010931, abs=1e-5)
        assert chloroform[2][1] == pytest.approx(0.959875, abs=_ABUNDANCE_ABS)


# ---------------------------------------------------------------------------
# 2. mzML ingestion - a standards-conformant document built independently
# ---------------------------------------------------------------------------
_MZML_NS = "http://psi.hupo.org/ms/mzml"


class _MzMLWriter:
    """A minimal mzML 1.1.0 writer owned by this test module.

    It intentionally does not reuse ``tests/conftest.py``'s builders: the point
    of this class is to be a producer MSMCP does not control, so that ingestion
    is validated against an external document rather than one whose expectations
    and implementation share a lineage.
    """

    def __init__(self) -> None:
        self._spectra: list[str] = []
        self._count = 0

    @staticmethod
    def _b64(values: list[float], *, precision: str, compress: bool) -> str:
        dtype = "<f4" if precision == "32" else "<f8"
        raw = np.asarray(values, dtype=dtype).tobytes()
        if compress:
            raw = zlib.compress(raw)
        return base64.b64encode(raw).decode()

    def _array(
        self,
        accession: str,
        name: str,
        values: list[float],
        *,
        precision: str,
        compress: bool,
    ) -> str:
        payload = self._b64(values, precision=precision, compress=compress)
        precision_acc = "MS:1000521" if precision == "32" else "MS:1000523"
        precision_name = "32-bit float" if precision == "32" else "64-bit float"
        compression_acc = "MS:1000574" if compress else "MS:1000576"
        compression_name = "zlib compression" if compress else "no compression"
        return (
            f'<binaryDataArray encodedLength="{len(payload)}">'
            f'<cvParam cvRef="MS" accession="{accession}" name="{name}" value=""/>'
            f'<cvParam cvRef="MS" accession="{precision_acc}" name="{precision_name}" value=""/>'
            f'<cvParam cvRef="MS" accession="{compression_acc}" name="{compression_name}" value=""/>'
            f"<binary>{payload}</binary></binaryDataArray>"
        )

    def add_spectrum(
        self,
        mz: list[float],
        intensity: list[float],
        *,
        ms_level: int = 1,
        rt_seconds: float | None = None,
        precursor_mz: float | None = None,
        polarity: str | None = None,
        precision: str = "64",
        compress: bool = False,
    ) -> None:
        index = self._count
        self._count += 1

        polarity_xml = ""
        if polarity == "positive":
            polarity_xml = (
                '<cvParam cvRef="MS" accession="MS:1000130" '
                'name="positive scan" value=""/>'
            )
        elif polarity == "negative":
            polarity_xml = (
                '<cvParam cvRef="MS" accession="MS:1000129" '
                'name="negative scan" value=""/>'
            )

        scan_xml = ""
        if rt_seconds is not None:
            scan_xml = (
                f'<scanList count="1"><scan>'
                f'<cvParam cvRef="MS" accession="MS:1000016" name="scan start time" '
                f'value="{rt_seconds}" unitAccession="MS:1000038" unitName="second"/>'
                f"</scan></scanList>"
            )

        precursor_xml = ""
        if precursor_mz is not None:
            precursor_xml = (
                f'<precursorList count="1"><precursor><selectedIonList count="1">'
                f'<selectedIon><cvParam cvRef="MS" accession="MS:1000744" '
                f'name="selected ion m/z" value="{precursor_mz}"/></selectedIon>'
                f"</selectedIonList></precursor></precursorList>"
            )

        self._spectra.append(
            f'<spectrum index="{index}" id="scan={index}" '
            f'defaultArrayLength="{len(mz)}">'
            f'<cvParam cvRef="MS" accession="MS:1000511" name="ms level" value="{ms_level}"/>'
            f"{polarity_xml}{scan_xml}{precursor_xml}"
            f'<binaryDataArrayList count="2">'
            f"{self._array('MS:1000514', 'm/z array', mz, precision=precision, compress=compress)}"
            f"{self._array('MS:1000515', 'intensity array', intensity, precision=precision, compress=compress)}"
            f"</binaryDataArrayList></spectrum>"
        )

    def write(self, path: Path) -> Path:
        """Write the accumulated spectra as a complete mzML document."""
        document = (
            f'<?xml version="1.0" encoding="utf-8"?>'
            f'<mzML xmlns="{_MZML_NS}" version="1.1.0">'
            f'<run id="validation">'
            f'<spectrumList count="{len(self._spectra)}" '
            f'defaultDataProcessingRef="dp">'
            f"{''.join(self._spectra)}"
            f"</spectrumList></run></mzML>"
        )
        path.write_text(document, encoding="utf-8")
        return path


_CAFFEINE_MZ = [110.0713, 120.0808, 136.0757]
_CAFFEINE_INTENSITY = [40.0, 100.0, 60.0]


class TestMzMLIndependentDocument:
    """Ingestion of an independently written mzML document."""

    def test_peaks_and_metadata_round_trip_exactly(self, tmp_path: Path) -> None:
        writer = _MzMLWriter()
        writer.add_spectrum(
            _CAFFEINE_MZ,
            _CAFFEINE_INTENSITY,
            ms_level=2,
            rt_seconds=120.0,
            precursor_mz=195.0877,
            polarity="positive",
        )
        path = writer.write(tmp_path / "caffeine.mzML")

        spectra = list(iter_spectra(path))
        assert len(spectra) == 1
        spectrum = spectra[0]

        # float64 values are carried through bit-exactly; the reader must not
        # reorder, rescale or round the peak arrays.
        assert np.array_equal(spectrum.mz, np.asarray(_CAFFEINE_MZ, dtype=np.float64))
        assert np.array_equal(
            spectrum.intensity, np.asarray(_CAFFEINE_INTENSITY, dtype=np.float64)
        )
        assert spectrum.n_peaks == 3
        assert spectrum.tic == pytest.approx(200.0, abs=1e-9)
        assert spectrum.index == 0
        assert spectrum.ms_level == 2
        assert spectrum.precursor_mz == pytest.approx(195.0877)
        # 120 s is normalised to minutes, per the Spectrum contract.
        assert spectrum.retention_time == pytest.approx(2.0)
        assert spectrum.polarity == "positive"

    def test_ms1_spectrum_with_no_precursor(self, tmp_path: Path) -> None:
        writer = _MzMLWriter()
        writer.add_spectrum([50.0, 51.0], [1.0, 2.0], ms_level=1, rt_seconds=0.0)
        path = writer.write(tmp_path / "ms1.mzML")

        spectrum = next(iter_spectra(path))
        assert spectrum.ms_level == 1
        assert spectrum.precursor_mz is None
        assert spectrum.polarity is None  # never a guessed default
        assert spectrum.retention_time == pytest.approx(0.0)

    def test_negative_polarity_is_read_from_the_negative_scan_cvparam(
        self, tmp_path: Path
    ) -> None:
        writer = _MzMLWriter()
        writer.add_spectrum([100.0], [5.0], ms_level=2, polarity="negative")
        path = writer.write(tmp_path / "neg.mzML")
        assert next(iter_spectra(path)).polarity == "negative"

    def test_32_bit_precision_is_decoded_within_float32_tolerance(
        self, tmp_path: Path
    ) -> None:
        writer = _MzMLWriter()
        writer.add_spectrum([100.0, 200.5], [10.0, 20.0], precision="32")
        path = writer.write(tmp_path / "f32.mzML")
        spectrum = next(iter_spectra(path))
        assert np.allclose(spectrum.mz, [100.0, 200.5], atol=1e-3)
        assert np.allclose(spectrum.intensity, [10.0, 20.0], atol=1e-6)

    def test_zlib_compressed_arrays_are_decoded(self, tmp_path: Path) -> None:
        writer = _MzMLWriter()
        writer.add_spectrum(_CAFFEINE_MZ, _CAFFEINE_INTENSITY, compress=True)
        path = writer.write(tmp_path / "zlib.mzML")
        spectrum = next(iter_spectra(path))
        assert np.array_equal(spectrum.mz, np.asarray(_CAFFEINE_MZ, dtype=np.float64))
        assert np.array_equal(
            spectrum.intensity, np.asarray(_CAFFEINE_INTENSITY, dtype=np.float64)
        )

    def test_declared_spectrum_count_matches_the_written_document(
        self, tmp_path: Path
    ) -> None:
        from msmcp.mzml import declared_spectrum_count

        writer = _MzMLWriter()
        for i in range(4):
            writer.add_spectrum([100.0 + i], [1.0])
        path = writer.write(tmp_path / "many.mzML")
        assert declared_spectrum_count(path) == 4


# ---------------------------------------------------------------------------
# 3. A real MCP session over the stdio transport
# ---------------------------------------------------------------------------
_NEWLINE = "\n"


class _StdioHost:
    """Drive a real ``msmcp.server`` child process over the stdio transport."""

    def __init__(self, *, allowed_root: Path, cwd: Path) -> None:
        env = dict(os.environ)
        env["MSMCP_ALLOWED_ROOT"] = str(allowed_root)
        self._proc = subprocess.Popen(
            [sys.executable, "-m", "msmcp.server"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
            cwd=str(cwd),
        )
        assert self._proc.stdin and self._proc.stdout and self._proc.stderr
        self._stdout: list[str] = []
        self._stderr: list[str] = []
        self._readers = [
            threading.Thread(
                target=self._drain, args=(self._proc.stdout, self._stdout), daemon=True
            ),
            threading.Thread(
                target=self._drain, args=(self._proc.stderr, self._stderr), daemon=True
            ),
        ]
        for reader in self._readers:
            reader.start()
        self._next_id = 1

    @staticmethod
    def _drain(stream: Any, sink: list[str]) -> None:
        for line in stream:
            sink.append(line)

    def send(self, method: str, params: dict[str, Any]) -> int:
        """Send one request, returning the id its response will carry."""
        request_id = self._next_id
        self._next_id += 1
        message = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params,
        }
        assert self._proc.stdin
        self._proc.stdin.write(json.dumps(message) + _NEWLINE)
        self._proc.stdin.flush()
        return request_id

    def notify(self, method: str, params: dict[str, Any]) -> None:
        assert self._proc.stdin
        message = {"jsonrpc": "2.0", "method": method, "params": params}
        self._proc.stdin.write(json.dumps(message) + _NEWLINE)
        self._proc.stdin.flush()

    def call_tool(self, name: str, arguments: dict[str, Any]) -> int:
        return self.send("tools/call", {"name": name, "arguments": arguments})

    def responses(self, ids: set[int], *, timeout: float = 60.0) -> dict[int, dict]:
        """Wait until every requested id has a response, then return them."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            parsed = [json.loads(line) for line in self._stdout if line.strip()]
            by_id = {r.get("id"): r for r in parsed}
            if ids <= set(by_id):
                return {i: by_id[i] for i in ids}
            time.sleep(0.05)
        pytest.fail(
            f"timeout waiting for responses {sorted(ids)}; "
            f"stdout={self._stdout[-5:]!r} stderr={self._stderr[-5:]!r}"
        )

    def close(self) -> None:
        if self._proc.poll() is None:
            assert self._proc.stdin
            self._proc.stdin.close()
            try:
                self._proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                self._proc.wait(timeout=30)
        for reader in self._readers:
            reader.join(timeout=5)


def _tool_payload(response: dict[str, Any]) -> dict[str, Any]:
    """Extract a tool's structured result from its JSON-RPC response."""
    assert "error" not in response, response
    result = response["result"]
    assert not result.get("isError"), result
    if "structuredContent" in result:
        return dict(result["structuredContent"])
    return json.loads(result["content"][0]["text"])


def _sha256(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def test_full_mcp_workflow_over_stdio(tmp_path: Path) -> None:
    """load_spectrum -> summarise_reference -> annotate_isotopes -> release, for real.

    This is the end-to-end claim that the in-process suite cannot make: a real
    server process, a real JSON-RPC session, a real mzML file written by this
    module, and a server-side reference round-tripped through the wire.
    """
    writer = _MzMLWriter()
    writer.add_spectrum(
        _CAFFEINE_MZ,
        _CAFFEINE_INTENSITY,
        ms_level=2,
        rt_seconds=120.0,
        precursor_mz=195.0877,
        polarity="positive",
    )
    mzml_path = writer.write(tmp_path / "caffeine.mzML")
    expected_digest = _sha256(mzml_path)

    host = _StdioHost(allowed_root=tmp_path, cwd=tmp_path)
    try:
        init_id = host.send(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "scientific-validation", "version": "1.0"},
            },
        )
        host.notify("notifications/initialized", {})

        load_id = host.call_tool(
            "load_spectrum",
            {"file_path": str(mzml_path), "spectrum_index": 0},
        )

        first_responses = host.responses({init_id, load_id})
        init_resp, load_resp = first_responses[init_id], first_responses[load_id]
        assert init_resp["result"]["serverInfo"]["name"] == "MSMCP-MassFlow-Adapter"

        loaded = _tool_payload(load_resp)
        reference = loaded["reference"]
        assert reference.startswith("ptr:spectrum:")

        # The structured result describes the spectrum without containing it.
        assert loaded["format"] == "mzML"
        assert loaded["backend"] == "msmcp.mzml"
        assert loaded["n_peaks"] == 3
        assert loaded["tic"] == pytest.approx(200.0, abs=1e-9)
        assert loaded["ms_level"] == 2
        assert loaded["precursor_mz"] == pytest.approx(195.0877)
        assert loaded["retention_time"] == pytest.approx(2.0)

        # Provenance must name the operation and carry an *independently
        # computed* content digest of the exact file that was read.
        provenance = loaded["provenance"]
        assert provenance["operation"] == "load_spectrum"
        assert provenance["parameters"]["reader_backend"] == "msmcp.mzml"
        assert len(provenance["sources"]) == 1
        assert provenance["sources"][0]["digest"] == expected_digest

        summary_id = host.call_tool("summarise_reference", {"reference": reference})
        isotope_id = host.call_tool(
            "annotate_isotopes", {"identifier": "C8H10N4O2", "is_smiles": False}
        )
        release_id = host.call_tool("release_reference", {"reference": reference})

        summary_resp = host.responses({summary_id})[summary_id]
        isotope_resp = host.responses({isotope_id})[isotope_id]
        release_resp = host.responses({release_id})[release_id]

        # The reference reads back the peaks that were loaded (round-trip).
        summary = summary_resp["result"]["content"][0]["text"]
        assert reference in summary
        assert "TIC recomputed from stored peaks: 200.00" in summary
        assert "120.0808" in summary  # the base peak, from a *different* call
        assert f"digest={expected_digest}" in summary

        # The cheminformatics tool answers over the same session.
        isotope_text = isotope_resp["result"]["content"][0]["text"]
        assert "## Isotope Pattern: C8H10N4O2" in isotope_text
        assert "194.0804 Da" in isotope_text

        release_text = release_resp["result"]["content"][0]["text"]
        assert "Released" in release_text
    finally:
        host.close()
