"""Exercise every core MSMCP tool through an installed stdio MCP server.

This is a qualification harness, not production code. It writes only synthetic
MSP/MGF/mzML fixtures to a temporary directory and expects the server command
to point at a separately installed wheel or sdist environment.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import re
import struct
import tempfile
import time
from pathlib import Path
from typing import Any

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

EXPECTED_TOOLS = {
    "ping",
    "load_mzml_summary",
    "list_spectra",
    "extract_chromatograms",
    "load_spectrum",
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

MS2_PEAKS = [
    (110.0713, 40.0),
    (120.0808, 100.0),
    (136.0757, 60.0),
    (138.0662, 420.0),
    (500.1, 55.0),
]
QUERY_MGF = """BEGIN IONS
TITLE=Caffeine MS2 query
PEPMASS=195.0877
MSLEVEL=2
IONMODE=POSITIVE
CHARGE=1+
110.0713 40.0
120.0808 100.0
136.0757 60.0
138.0662 420.0
500.1 55.0
END IONS
"""
MS1_MGF = """BEGIN IONS
TITLE=MS1 survey scan
PEPMASS=195.0877
MSLEVEL=1
IONMODE=POSITIVE
110.0713 40.0
120.0808 100.0
END IONS
"""
LIBRARY_MSP = """Name: Caffeine
Formula: C8H10N4O2
PrecursorMZ: 195.0877
Ion_mode: P
Charge: 1
Num Peaks: 5
110.0713 40.0
120.0808 100.0
136.0757 60.0
138.0662 420.0
500.1 55.0

Name: Wrong precursor
PrecursorMZ: 900.0000
Ion_mode: P
Charge: 1
Num Peaks: 5
110.0713 40.0
120.0808 100.0
136.0757 60.0
138.0662 420.0
500.1 55.0

Name: Wrong polarity
PrecursorMZ: 195.0877
Ion_mode: N
Charge: 1
Num Peaks: 5
110.0713 40.0
120.0808 100.0
136.0757 60.0
138.0662 420.0
500.1 55.0
"""


def _array(accession: str, name: str, values: list[float]) -> str:
    payload = base64.b64encode(struct.pack(f"<{len(values)}d", *values)).decode("ascii")
    return (
        f'<binaryDataArray encodedLength="{len(payload)}">'
        f'<cvParam cvRef="MS" accession="{accession}" name="{name}" value=""/>'
        '<cvParam cvRef="MS" accession="MS:1000523" name="64-bit float" value=""/>'
        '<cvParam cvRef="MS" accession="MS:1000576" name="no compression" value=""/>'
        f"<binary>{payload}</binary></binaryDataArray>"
    )


def _spectrum(
    index: int,
    ms_level: int,
    retention_seconds: float,
    mz: list[float],
    intensity: list[float],
    precursor_mz: float | None = None,
) -> str:
    precursor = ""
    if precursor_mz is not None:
        precursor = (
            '<precursorList count="1"><precursor><selectedIonList count="1">'
            f'<selectedIon><cvParam cvRef="MS" accession="MS:1000744" '
            f'name="selected ion m/z" value="{precursor_mz}"/>'
            '<cvParam cvRef="MS" accession="MS:1000041" name="charge state" value="1"/>'
            "</selectedIon></selectedIonList></precursor></precursorList>"
        )
    return (
        f'<spectrum index="{index}" id="scan={index}" defaultArrayLength="{len(mz)}">'
        f'<cvParam cvRef="MS" accession="MS:1000511" name="ms level" value="{ms_level}"/>'
        '<cvParam cvRef="MS" accession="MS:1000130" name="positive scan" value=""/>'
        '<cvParam cvRef="MS" accession="MS:1000127" name="centroid spectrum" value=""/>'
        '<scanList count="1"><scan>'
        f'<cvParam cvRef="MS" accession="MS:1000016" name="scan start time" '
        f'value="{retention_seconds}" unitAccession="UO:0000010" unitName="second"/>'
        f"</scan></scanList>{precursor}"
        '<binaryDataArrayList count="2">'
        f"{_array('MS:1000514', 'm/z array', mz)}"
        f"{_array('MS:1000515', 'intensity array', intensity)}"
        "</binaryDataArrayList></spectrum>"
    )


def _write_mzml(path: Path) -> None:
    spectra = [
        _spectrum(0, 1, 60.0, [100.0, 200.0, 300.0], [10.0, 20.0, 30.0]),
        _spectrum(
            1,
            2,
            120.0,
            [mz for mz, _ in MS2_PEAKS],
            [intensity for _, intensity in MS2_PEAKS],
            195.0877,
        ),
    ]
    xml = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<mzML xmlns="http://psi.hupo.org/ms/mzml" version="1.1.0">'
        '<run id="qualification"><spectrumList count="2" defaultDataProcessingRef="dp">'
        + "".join(spectra)
        + "</spectrumList></run></mzML>"
    )
    path.write_text(xml, encoding="utf-8")


def _write_fixtures(root: Path) -> dict[str, Path]:
    paths = {
        "mzml": root / "acquisition.mzML",
        "query": root / "query.mgf",
        "ms1": root / "ms1.mgf",
        "library": root / "library.msp",
        "malformed": root / "malformed.msp",
        "large_library": root / "large-library.msp",
    }
    _write_mzml(paths["mzml"])
    paths["query"].write_text(QUERY_MGF, encoding="utf-8")
    paths["ms1"].write_text(MS1_MGF, encoding="utf-8")
    paths["library"].write_text(LIBRARY_MSP, encoding="utf-8")
    paths["malformed"].write_text(
        "Name: Broken\nNum Peaks: 5\n100 1\n", encoding="utf-8"
    )
    with paths["large_library"].open("w", encoding="utf-8") as handle:
        for index in range(50_000):
            handle.write(
                f"Name: Cancel-{index}\nPrecursorMZ: 195.0877\nIon_mode: P\n"
                "Num Peaks: 5\n110.0713 40\n120.0808 100\n136.0757 60\n"
                "138.0662 420\n500.1 55\n\n"
            )
    return paths


def _text(result: Any) -> str:
    return "\n".join(
        item.text for item in result.content if getattr(item, "type", "") == "text"
    )


def _job_id(text: str) -> str:
    match = re.search(r"\*\*Job ID:\*\*\s*`([0-9a-f]{32})`", text)
    if match is None:
        raise AssertionError(f"search dispatch did not return a job id: {text}")
    return match.group(1)


async def _poll(session: ClientSession, job_id: str, *, cancelled: bool = False) -> str:
    deadline = time.monotonic() + 60.0
    while time.monotonic() < deadline:
        response = await session.call_tool(
            "check_search_status", {"job_id": job_id, "full_report": True}
        )
        text = _text(response)
        if response.is_error:
            raise AssertionError(f"status polling failed: {text}")
        if cancelled and "Cancelled" in text:
            return text
        if not cancelled and "Spectral Library Search Results" in text:
            return text
        if not cancelled and "Failed" in text:
            raise AssertionError(f"search job failed: {text}")
        await asyncio.sleep(0.05)
    raise TimeoutError(f"job {job_id} did not reach the expected terminal state")


async def qualify(server_command: str) -> dict[str, Any]:
    evidence: dict[str, Any] = {"fixture_class": "synthetic software/protocol fixtures"}
    with tempfile.TemporaryDirectory(prefix="msmcp-release-qualification-") as raw_root:
        root = Path(raw_root).resolve()
        files = _write_fixtures(root)
        outside = root.parent / f"{root.name}-outside.mgf"
        outside.write_text(QUERY_MGF, encoding="utf-8")
        symlink = root / "outside-link.mgf"
        symlink.symlink_to(outside)
        environment = dict(os.environ)
        environment["MSMCP_ALLOWED_ROOT"] = str(root)
        environment["MSMCP_MAX_SPECTRA"] = "2"
        environment.pop("MSMCP_EMBEDDING_BACKEND", None)
        environment.pop("PYTHONPATH", None)
        environment.pop("VIRTUAL_ENV", None)
        server = StdioServerParameters(
            command=server_command,
            args=[],
            env=environment,
            cwd=str(root),
        )
        async with (
            stdio_client(server) as (read, write),
            ClientSession(read, write) as session,
        ):
            initialized = await session.initialize()
            evidence["server"] = {
                "name": initialized.server_info.name,
                "version": initialized.server_info.version,
                "protocol": initialized.protocol_version,
            }
            tools = await session.list_tools()
            names = {tool.name for tool in tools.tools}
            if names != EXPECTED_TOOLS:
                raise AssertionError(
                    f"unexpected tool list: missing={sorted(EXPECTED_TOOLS - names)}, "
                    f"extra={sorted(names - EXPECTED_TOOLS)}"
                )
            evidence["advertised_core_tools"] = sorted(names)
            evidence["all_tool_schemas_present"] = all(
                tool.input_schema for tool in tools.tools
            )
            evidence["chromatogram_output_schema"] = next(
                tool.output_schema is not None
                for tool in tools.tools
                if tool.name == "extract_chromatograms"
            )

            async def call(name: str, arguments: dict[str, Any]) -> Any:
                response = await session.call_tool(name, arguments)
                if response.is_error:
                    raise AssertionError(f"{name} failed: {_text(response)}")
                return response

            ping = await call("ping", {})
            evidence["ping"] = _text(ping)

            summary = await call(
                "load_mzml_summary",
                {"file_path": str(files["mzml"]), "max_spectra": 2},
            )
            evidence["mzml_summary"] = _text(summary)
            assert "Summarised 2 of 2 total spectra." in _text(summary)

            index = await call(
                "list_spectra",
                {"file_path": str(files["mzml"]), "ms_level": 2, "max_spectra": 2},
            )
            evidence["spectrum_index"] = _text(index)
            assert "index" in _text(index).lower() and "MS2" in _text(index)

            chromatogram = await call(
                "extract_chromatograms",
                {
                    "file_path": str(files["mzml"]),
                    "targets_mz": [100.0],
                    "tolerance": 0.0,
                    "tolerance_unit": "Da",
                    "ms_level": 1,
                    "max_spectra": 10,
                },
            )
            chrom_data = chromatogram.structured_content
            assert chrom_data is not None
            assert chrom_data["completeness"]["full_acquisition_processed"] is True
            assert len(chrom_data["chromatograms"]["retention_times_min"]) == 1
            evidence["chromatogram"] = {
                "schema_version": chrom_data["schema_version"],
                "scanned_spectra": chrom_data["acquisition"]["scanned_spectra"],
                "returned_points": chrom_data["completeness"]["returned_points"],
                "source_digest": chrom_data["source"]["digest"],
            }

            loaded = await call(
                "load_spectrum", {"file_path": str(files["mzml"]), "spectrum_index": 1}
            )
            payload = loaded.structured_content or {}
            pointer = str(payload["reference"])
            assert pointer.startswith("ptr:spectrum:")
            assert "mz" not in payload and "intensity" not in payload
            assert len(json.dumps(payload, separators=(",", ":"))) < 4096
            evidence["load_spectrum"] = {
                "reference": pointer,
                "ms_level": payload["ms_level"],
                "n_peaks": payload["n_peaks"],
                "source_digest": payload["provenance"]["sources"][0]["digest"],
            }

            summary_ref = await call("summarise_reference", {"reference": pointer})
            assert "Provenance: load_spectrum" in _text(summary_ref)
            evidence["summarise_reference"] = "passed"
            released = await call("release_reference", {"reference": pointer})
            evidence["release_reference"] = _text(released)
            expired = await session.call_tool(
                "summarise_reference", {"reference": pointer}
            )
            assert expired.is_error
            evidence["released_reference_rejected"] = True

            adduct = await call("predict_adduct_offset", {"adduct_string": "[M+2H]2+"})
            assert "+2.014553 Da" in _text(adduct)
            evidence["predict_adduct_offset"] = "passed"

            isotope = await call("annotate_isotopes", {"identifier": "C6H12O6"})
            assert "180.0634 Da" in _text(isotope)
            evidence["annotate_isotopes"] = "passed"

            precursor = await call(
                "validate_precursor",
                {
                    "theoretical_mass": 195.0877,
                    "experimental_mass": 195.0877,
                    "instrument_class": "generic",
                },
            )
            assert "VALIDATION PASSED" in _text(precursor)
            evidence["validate_precursor"] = "passed"

            cosine = await call(
                "compute_cosine",
                {
                    "query_peaks": [[100.0, 2.0], [200.0, 4.0]],
                    "reference_peaks": [[100.0, 2.0], [200.0, 4.0]],
                },
            )
            assert "1.0000" in _text(cosine)
            evidence["compute_cosine"] = "passed"

            qc = await call("generate_qc_summary", {"file_path": str(files["mzml"])})
            assert "Spectra analysed" in _text(qc)
            evidence["generate_qc_summary"] = _text(qc).splitlines()[0]

            dispatched = await call(
                "search_library",
                {
                    "database_file": str(files["library"]),
                    "experimental_file": str(files["query"]),
                    "spectrum_index": 0,
                    "scoring_method": "classical",
                    "instrument_class": "generic",
                },
            )
            job = _job_id(_text(dispatched))
            report = await _poll(session, job)
            assert "Caffeine" in report and "SIGNIFICANCE UNCALIBRATED" in report
            assert "| p-value" not in report.lower()
            evidence["search_library"] = {
                "job_id": job,
                "caffeine_ranked": "| 1 | Caffeine |" in report,
                "significance_disclosed": "SIGNIFICANCE UNCALIBRATED" in report,
                "provenance": "Provenance: search_library" in report,
            }
            status = await call(
                "check_search_status", {"job_id": job, "full_report": True}
            )
            assert "Spectral Library Search Results" in _text(status)
            evidence["check_search_status"] = "completed/pollable"

            cancellation_dispatch = await call(
                "search_library",
                {
                    "database_file": str(files["large_library"]),
                    "experimental_file": str(files["query"]),
                    "spectrum_index": 0,
                    "scoring_method": "classical",
                    "instrument_class": "generic",
                    "chunk_size": 100,
                },
            )
            cancellation_job = _job_id(_text(cancellation_dispatch))
            cancellation = await call("cancel_search", {"job_id": cancellation_job})
            cancellation_text = _text(cancellation)
            if "Search Cancelled" not in cancellation_text:
                raise AssertionError(
                    f"job completed before cancellation: {cancellation_text}"
                )
            cancelled_status = await _poll(session, cancellation_job, cancelled=True)
            assert "Cancelled" in cancelled_status
            evidence["cancel_search"] = {"requested": True, "status": "cancelled"}

            out_of_root = await session.call_tool(
                "load_spectrum", {"file_path": str(outside), "spectrum_index": 0}
            )
            symlink_escape = await session.call_tool(
                "load_spectrum", {"file_path": str(symlink), "spectrum_index": 0}
            )
            malformed_library = await session.call_tool(
                "search_library",
                {
                    "database_file": str(files["malformed"]),
                    "experimental_file": str(files["query"]),
                    "spectrum_index": 0,
                },
            )
            ms1_search = await session.call_tool(
                "search_library",
                {
                    "database_file": str(files["library"]),
                    "experimental_file": str(files["ms1"]),
                },
            )
            out_of_budget = await session.call_tool(
                "load_spectrum", {"file_path": str(files["mzml"]), "spectrum_index": 2}
            )
            failures = {
                "outside_root": out_of_root,
                "symlink_escape": symlink_escape,
                "malformed_library": malformed_library,
                "ms1_search": ms1_search,
                "spectrum_budget": out_of_budget,
            }
            if not all(result.is_error for result in failures.values()):
                raise AssertionError(
                    {name: _text(result) for name, result in failures.items()}
                )
            evidence["bounded_errors"] = sorted(failures)
            final_ping = await call("ping", {})
            evidence["stdio_session_survived_errors"] = not final_ping.is_error
        outside.unlink(missing_ok=True)
    return evidence


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server-command", required=True)
    arguments = parser.parse_args()
    print(
        json.dumps(
            asyncio.run(qualify(arguments.server_command)), indent=2, sort_keys=True
        )
    )


if __name__ == "__main__":
    main()
