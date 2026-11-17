"""Run three offline deterministic demonstration workflows over MSMCP stdio.

Demonstrates MSMCP 1.0.2 tool mechanics across:
1. Exact-mass chemistry (isotopes, adduct offset, precursor ppm gate);
2. Synthetic MS/MS spectral library search and cosine similarity;
3. Synthetic LC-MS scan listing, chromatogram extraction, and QC summary.

All spectral fixtures used here are synthetic demonstration inputs rather than
measured laboratory acquisitions; this script checks deterministic software
behaviour, not analytical validation.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import shutil
import struct
import sys
import tempfile
import time
from base64 import b64encode
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import CallToolResult

DEFAULT_EXAMPLES_DIR = Path(__file__).resolve().parent
POLL_TIMEOUT_S = 120.0
POLL_INTERVAL_S = 0.1

EXPECTED_TOOLS = {
    "ping",
    "load_mzml_summary",
    "list_spectra",
    "load_spectrum",
    "extract_chromatograms",
    "summarise_reference",
    "release_reference",
    "predict_adduct_offset",
    "annotate_isotopes",
    "validate_precursor",
    "compute_cosine",
    "generate_qc_summary",
    "search_library",
    "check_search_status",
    "cancel_search",
}

CAFFEINE_PEAKS = [
    [110.0713, 40.0],
    [120.0808, 100.0],
    [136.0757, 60.0],
    [138.0662, 420.0],
]


def _encode_binary_array(kind: str, values: list[float]) -> str:
    payload = b64encode(struct.pack("<" + "d" * len(values), *values)).decode("ascii")
    accession, name = (
        ("MS:1000514", "m/z array")
        if kind == "mz"
        else ("MS:1000515", "intensity array")
    )
    return (
        f'<binaryDataArray encodedLength="{len(payload)}">'
        f'<cvParam cvRef="MS" accession="{accession}" name="{name}"/>'
        '<cvParam cvRef="MS" accession="MS:1000523" name="64-bit float"/>'
        '<cvParam cvRef="MS" accession="MS:1000576" name="no compression"/>'
        f"<binary>{payload}</binary>"
        "</binaryDataArray>"
    )


def write_synthetic_mzml(dest: Path) -> Path:
    """Write a minimal synthetic centroid mzML fixture (41 MS1 + 4 MS2 scans).

    Intensities are constructed Gaussians for software mechanics verification,
    not simulated measurement uncertainty or real chromatographic data.
    """
    spectra_xml: list[str] = []
    for n in range(41):
        rt_min = n * 0.1
        peak_195 = 1000.0 * math.exp(-0.5 * ((rt_min - 1.5) / 0.32) ** 2)
        peak_250 = 650.0 * math.exp(-0.5 * ((rt_min - 2.7) / 0.45) ** 2)
        scans: list[tuple[int, list[float], list[float], float | None, float]] = [
            (1, [100.0, 195.0877, 250.1234], [50.0, peak_195, peak_250], None, rt_min),
        ]
        if n in (10, 15, 25, 30):
            scans.append(
                (2, [110.0713, 138.0662], [20.0, 100.0], 195.0877, rt_min + 0.02)
            )

        for level, mz_vals, inten_vals, precursor_mz, scan_rt in scans:
            idx = len(spectra_xml)
            precursor_xml = (
                ""
                if precursor_mz is None
                else (
                    '<precursorList count="1"><precursor><selectedIonList count="1">'
                    f'<selectedIon><cvParam cvRef="MS" accession="MS:1000744" '
                    f'name="selected ion m/z" value="{precursor_mz}"/></selectedIon>'
                    "</selectedIonList></precursor></precursorList>"
                )
            )
            spectra_xml.append(
                f'<spectrum index="{idx}" id="scan={idx}" '
                f'defaultArrayLength="{len(mz_vals)}">'
                f'<cvParam cvRef="MS" accession="MS:1000511" name="ms level" value="{level}"/>'
                '<cvParam cvRef="MS" accession="MS:1000130" name="positive scan"/>'
                '<cvParam cvRef="MS" accession="MS:1000127" name="centroid spectrum"/>'
                '<scanList count="1"><scan>'
                f'<cvParam cvRef="MS" accession="MS:1000016" name="scan start time" '
                f'value="{scan_rt:.2f}" unitAccession="UO:0000031" unitName="minute"/>'
                "</scan></scanList>"
                f"{precursor_xml}"
                '<binaryDataArrayList count="2">'
                f"{_encode_binary_array('mz', mz_vals)}"
                f"{_encode_binary_array('intensity', inten_vals)}"
                "</binaryDataArrayList></spectrum>"
            )

    xml = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<mzML xmlns="http://psi.hupo.org/ms/mzml" version="1.1.0">'
        f'<run id="synthetic"><spectrumList count="{len(spectra_xml)}">'
        f"{''.join(spectra_xml)}"
        "</spectrumList></run></mzML>"
    )
    dest.write_text(xml, encoding="utf-8")
    return dest


def _text_payload(result: CallToolResult) -> str:
    return "\n".join(
        text for block in result.content if (text := getattr(block, "text", None))
    )


async def _call_tool(
    session: ClientSession, name: str, arguments: dict[str, Any]
) -> tuple[str, dict[str, Any] | None]:
    result = await session.call_tool(name, arguments)
    text = _text_payload(result)
    if result.is_error:
        raise RuntimeError(f"Tool {name!r} failed: {text}")
    return text, result.structured_content


def _extract_job_id(dispatched_text: str) -> str:
    for line in dispatched_text.splitlines():
        if "Job ID" in line and "`" in line:
            return line.split("`")[1]
    raise RuntimeError(f"Could not locate Job ID in dispatch reply:\n{dispatched_text}")


async def _poll_search(session: ClientSession, job_id: str) -> str:
    deadline = time.monotonic() + POLL_TIMEOUT_S
    while time.monotonic() < deadline:
        text, _ = await _call_tool(
            session,
            "check_search_status",
            {"job_id": job_id, "full_report": True},
        )
        if "## Spectral Library Search Results" in text:
            return text
        if "❌ **Failed**" in text or "cancelled" in text.lower():
            raise RuntimeError(f"Search job {job_id} did not succeed:\n{text}")
        await asyncio.sleep(POLL_INTERVAL_S)
    raise TimeoutError(f"Search job {job_id} timed out after {POLL_TIMEOUT_S}s")


async def run_chemistry_workflow(session: ClientSession) -> dict[str, str]:
    """Run Workflow 1: caffeine isotopes, [M+H]+ adduct offset, precursor gate."""
    isotopes, _ = await _call_tool(
        session,
        "annotate_isotopes",
        {"identifier": "C8H10N4O2", "isotopologues": 4},
    )
    adduct, _ = await _call_tool(
        session,
        "predict_adduct_offset",
        {"adduct_string": "[M+H]+"},
    )
    # Sum of the displayed rounded neutral monoisotopic mass (194.0804 Da) and
    # displayed rounded [M+H]+ offset (+1.007276 Da) = 195.087676 m/z.
    mass_pass, _ = await _call_tool(
        session,
        "validate_precursor",
        {
            "theoretical_mass": 195.087676,
            "experimental_mass": 195.0877,
            "instrument_class": "orbitrap",
        },
    )
    mass_reject, _ = await _call_tool(
        session,
        "validate_precursor",
        {
            "theoretical_mass": 195.087676,
            "experimental_mass": 195.09,
            "instrument_class": "orbitrap",
        },
    )
    return {
        "isotopes": isotopes,
        "adduct": adduct,
        "mass_pass": mass_pass,
        "mass_reject": mass_reject,
    }


async def run_library_workflow(
    session: ClientSession, query_path: Path, library_path: Path
) -> dict[str, Any]:
    """Run Workflow 2: synthetic caffeine MGF/MSP search and pairwise cosine."""
    loaded_text, _ = await _call_tool(
        session,
        "load_spectrum",
        {"file_path": str(query_path), "spectrum_index": 0},
    )
    loaded = json.loads(loaded_text)
    reference = str(loaded["reference"])

    dispatched, _ = await _call_tool(
        session,
        "search_library",
        {
            "spectrum_reference": reference,
            "database_file": str(library_path),
            "scoring_method": "classical",
            "instrument_class": "generic",
            "chunk_size": 100,
        },
    )
    job_id = _extract_job_id(dispatched)
    search_report = await _poll_search(session, job_id)
    cosine, _ = await _call_tool(
        session,
        "compute_cosine",
        {
            "query_peaks": CAFFEINE_PEAKS,
            "reference_peaks": CAFFEINE_PEAKS,
            "instrument_class": "generic",
        },
    )
    released, _ = await _call_tool(
        session,
        "release_reference",
        {"reference": reference},
    )
    return {
        "loaded": loaded,
        "dispatched": dispatched,
        "search_report": search_report,
        "cosine": cosine,
        "released": released,
    }


async def run_lcms_workflow(session: ClientSession, mzml_path: Path) -> dict[str, Any]:
    """Run Workflow 3: synthetic LC-MS spectrum listing, XICs, and QC summary."""
    spectra, _ = await _call_tool(
        session,
        "list_spectra",
        {"file_path": str(mzml_path), "max_spectra": 100},
    )
    chrom_text, chrom_structured = await _call_tool(
        session,
        "extract_chromatograms",
        {
            "file_path": str(mzml_path),
            "targets_mz": [195.0877, 250.1234],
            "tolerance": 5.0,
            "tolerance_unit": "ppm",
            "ms_level": 1,
            "max_spectra": 10000,
        },
    )
    qc, _ = await _call_tool(
        session,
        "generate_qc_summary",
        {"file_path": str(mzml_path), "instrument_class": "generic"},
    )
    return {
        "spectra": spectra,
        "chromatograms_text": chrom_text,
        "chromatograms": chrom_structured,
        "qc": qc,
    }


async def run_workflows(
    work_dir: Path,
    server_cmd: Sequence[str] | None = None,
    examples_dir: Path = DEFAULT_EXAMPLES_DIR,
) -> dict[str, Any]:
    """Stage fixtures in *work_dir* and run all three workflows over stdio."""
    work_dir = work_dir.resolve()
    work_dir.mkdir(parents=True, exist_ok=True)

    query_path = work_dir / "caffeine_spectrum.mgf"
    library_path = work_dir / "caffeine_library.msp"
    shutil.copy2(examples_dir / "caffeine_spectrum.mgf", query_path)
    shutil.copy2(examples_dir / "caffeine_library.msp", library_path)
    mzml_path = write_synthetic_mzml(work_dir / "synthetic_lcms.mzML")
    mzml_sha256 = hashlib.sha256(mzml_path.read_bytes()).hexdigest()

    cmd = (
        list(server_cmd)
        if server_cmd is not None
        else [sys.executable, "-m", "msmcp.server"]
    )
    env = dict(os.environ)
    env["MSMCP_ALLOWED_ROOT"] = str(work_dir)
    parameters = StdioServerParameters(command=cmd[0], args=cmd[1:], env=env)

    async with (
        stdio_client(parameters) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        advertised_tools = sorted(
            tool.name for tool in (await session.list_tools()).tools
        )
        chemistry = await run_chemistry_workflow(session)
        library = await run_library_workflow(session, query_path, library_path)
        lcms = await run_lcms_workflow(session, mzml_path)

    return {
        "tools": advertised_tools,
        "mzml_sha256": mzml_sha256,
        "chemistry": chemistry,
        "library": library,
        "lcms": lcms,
    }


def _print_summary(results: dict[str, Any]) -> None:
    print(f"Advertised tools: {len(results['tools'])}")
    print("\n=== Workflow 1: Exact-mass chemistry ===")
    print(results["chemistry"]["isotopes"])
    print(results["chemistry"]["adduct"])
    print(results["chemistry"]["mass_pass"])
    print(results["chemistry"]["mass_reject"])
    print("\n=== Workflow 2: Synthetic caffeine library search ===")
    print(results["library"]["search_report"])
    print(results["library"]["cosine"])
    print("\n=== Workflow 3: Synthetic LC-MS exploration ===")
    print(f"synthetic_lcms.mzML SHA-256: {results['mzml_sha256']}")
    print(results["lcms"]["qc"])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--server",
        help="Path or command name for an installed msmcp entry point "
        "(defaults to `python -m msmcp.server`).",
    )
    parser.add_argument(
        "--work-dir",
        type=Path,
        help="Directory for staged inputs and synthetic mzML "
        "(defaults to an ephemeral temporary directory).",
    )
    parser.add_argument(
        "--json-out",
        type=Path,
        help="Optional path to write full JSON workflow outputs.",
    )
    args = parser.parse_args()
    server_cmd = [args.server] if args.server else None

    if args.work_dir is not None:
        results = asyncio.run(run_workflows(args.work_dir, server_cmd=server_cmd))
    else:
        with tempfile.TemporaryDirectory(prefix="msmcp-examples-") as tmp:
            results = asyncio.run(run_workflows(Path(tmp), server_cmd=server_cmd))

    _print_summary(results)
    if args.json_out is not None:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(
            json.dumps(results, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )


if __name__ == "__main__":
    main()
