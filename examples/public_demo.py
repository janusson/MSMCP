"""Replay measured public data through a real stdio server; no LLM simulation.

Run from the repository: uv run python examples/public_demo.py --help.
Source attribution, constants and interpretation: docs/demonstration/README.md.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import math
import re
import struct
import subprocess
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET
import zlib
from collections import Counter
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ACQUISITION = "32Mix_Phala_Tyr_1ugml_1.mzML"
ACQUISITION_SHA = "7536385fb4ba7203aa39cc9874efcb3e0c4d7e92e2dec9b3958b7ddaba7d66c4"
RECORD = "MSBNK-BAFG-CSL23111015079"
RECORD_SHA = "bfabf5789fc94d3801755a4a3c47b59200a29334d0334e958ef293e32e2f0089"
RECORD_URL = (
    "https://raw.githubusercontent.com/MassBank/MassBank-data/2026.03/BAFG/"
    f"{RECORD}.txt"
)
NS = {"m": "http://psi.hupo.org/ms/mzml"}
ROOT = Path(__file__).resolve().parents[1]


def verified_bytes(path: Path, expected: str) -> bytes:
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != expected:
        raise ValueError(f"SHA-256 mismatch: {path.name}")
    return data


def fetch(path: Path, url: str, expected: str) -> None:
    if not path.exists():
        # A failed/interrupted download never replaces an existing input.
        temporary = path.with_suffix(path.suffix + ".part")
        with urllib.request.urlopen(url, timeout=120) as response:
            data = response.read()
        if hashlib.sha256(data).hexdigest() != expected:
            raise ValueError(f"Download checksum mismatch: {path.name}")
        temporary.write_bytes(data)
        temporary.rename(path)
    verified_bytes(path, expected)


def prepare(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    fetch(
        directory / ACQUISITION,
        f"https://zenodo.org/records/10043877/files/{ACQUISITION}?download=1",
        ACQUISITION_SHA,
    )
    fetch(directory / f"{RECORD}.txt", RECORD_URL, RECORD_SHA)
    source = verified_bytes(directory / f"{RECORD}.txt", RECORD_SHA).decode()
    # Only reformat the measured m/z and intensity columns. No generated peaks,
    # scaling, rounding, denoising or invented metadata.
    peak_section = source.split("PK$PEAK: m/z int. rel.int.\n", 1)[1]
    peaks = [line.split()[:2] for line in peak_section.split("//", 1)[0].splitlines()]
    peaks = [pair for pair in peaks if pair]
    match = re.search(r"PK\$NUM_PEAK: (\d+)", source)
    if match is None:
        raise ValueError("MassBank source has no peak count")
    count = int(match[1])
    if len(peaks) != count:
        raise ValueError("MassBank declared peak count disagrees with source")
    peak_text = "\n".join(" ".join(pair) for pair in peaks)
    attribution = (
        f"# Measured MassBank {RECORD}; release 2026.03.\n"
        "# Ole Lessmann; Kevin S. Jewell; Bjoern Ehlig; Arne Wick.\n"
        "# Copyright 2025 Federal Institute of Hydrology, Koblenz, Germany.\n"
        "# dl-de/by-2-0: https://www.govdata.de/dl-de/by-2-0\n"
        f"# Source: {RECORD_URL}\n"
        "# Changed: format conversion only; original m/z/intensity tokens retained.\n"
    )
    outputs = {
        "favipiravir.msp": (
            attribution + "Name: Favipiravir\nFormula: C5H4FN3O2\n"
            "PrecursorMZ: 156.0215\nIon_mode: NEGATIVE\n"
            f"DB#: {RECORD}\nNum Peaks: {count}\n{peak_text}\n\n"
        ),
        "favipiravir.mgf": (
            attribution + f"BEGIN IONS\nTITLE={RECORD}\nPEPMASS=156.0215\n"
            f"MSLEVEL=2\nCHARGE=1-\nRTINSECONDS=290.4\n{peak_text}\nEND IONS\n"
        ),
    }
    for name, content in outputs.items():
        destination = directory / name
        if destination.exists() and destination.read_text() != content:
            raise ValueError(f"Refusing to overwrite differing {name}")
        destination.write_text(content)
    print("Verified public acquisition and converted measured MassBank record.")


def independent_check(directory: Path) -> dict[str, Any]:
    """Read original XML and binary arrays without importing MSMCP's readers."""
    document = ET.fromstring(verified_bytes(directory / ACQUISITION, ACQUISITION_SHA))
    spectra = document.findall(".//m:spectrum", NS)
    spectrum = spectra[8]
    cv = {e.attrib["accession"]: e for e in spectrum.findall(".//m:cvParam", NS)}
    observed = Decimal(cv["MS:1000744"].attrib["value"])
    # NIST neutral isotope masses and CODATA 2022 proton mass, independently
    # transcribed; do not import the constants or formula parser under test.
    neutral = (
        6 * Decimal("12")
        + 18 * Decimal("1.00782503223")
        + 3 * Decimal("15.99491461957")
        + 3 * Decimal("27.97692653465")
    )
    expected = neutral + Decimal("1.0072764665789")
    ppm = (observed - expected) / expected * 10**6
    arrays: dict[str, tuple[float, ...]] = {}
    for array in spectrum.findall(".//m:binaryDataArray", NS):
        accessions = {e.attrib["accession"] for e in array.findall("m:cvParam", NS)}
        payload = base64.b64decode(array.findtext("m:binary", namespaces=NS) or "")
        if "MS:1000574" in accessions:
            payload = zlib.decompress(payload)
        code = "d" if "MS:1000523" in accessions else "f"
        values = struct.unpack(
            "<" + code * (len(payload) // struct.calcsize(code)), payload
        )
        arrays["mz" if "MS:1000514" in accessions else "intensity"] = values
    levels = Counter(
        e.attrib["value"]
        for s in spectra
        for e in s.findall("m:cvParam", NS)
        if e.attrib["accession"] == "MS:1000511"
    )
    return {
        "sha256": ACQUISITION_SHA,
        "bytes": (directory / ACQUISITION).stat().st_size,
        "total_spectra": len(spectra),
        "ms_levels": dict(levels),
        "index": 8,
        "native_id": spectrum.attrib["id"],
        "positive_polarity": "MS:1000130" in cv,
        "retention_time_min": cv["MS:1000016"].attrib["value"],
        "n_peaks": len(arrays["mz"]),
        "tic": math.fsum(arrays["intensity"]),
        "observed_mz": str(observed),
        "neutral_mass_da": str(neutral),
        "expected_mz": str(expected),
        "signed_error_ppm": str(ppm),
        "tolerance_ppm": 5,
        "mass_consistent": abs(ppm) <= 5,
        "identification_established": False,
    }


async def replay(directory: Path, output: Path) -> None:
    check = independent_check(directory)
    verified_bytes(directory / f"{RECORD}.txt", RECORD_SHA)
    # Reverify generated MSP/MGF bytes too; never replay a user-modified library.
    prepare(directory)
    output.mkdir(parents=True, exist_ok=False)
    entries: list[dict[str, Any]] = []
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "msmcp.server"],
        cwd=str(directory),
        env={
            "MSMCP_ALLOWED_ROOT": str(directory),
            "MSMCP_MAX_SPECTRA": "5000",
            "MSMCP_MAX_FILE_SIZE_BYTES": "524288000",
            "PYTHONPATH": str(ROOT / "src"),
        },
    )
    with (output / "server.stderr.txt").open("w") as errlog:
        async with stdio_client(parameters, errlog=errlog) as (reader, writer):
            async with ClientSession(reader, writer) as session:
                initialized = await session.initialize()

                async def call(name: str, **arguments: Any) -> dict[str, Any]:
                    result = await session.call_tool(name, arguments)
                    value = result.model_dump(
                        mode="json", by_alias=True, exclude_none=True
                    )
                    entries.append(
                        {"tool": name, "arguments": arguments, "result": value}
                    )
                    if result.is_error:
                        raise RuntimeError(f"Tool failed: {name}")
                    return value

                await call("load_mzml_summary", file_path=ACQUISITION, max_spectra=2)
                loaded = await call(
                    "load_spectrum", file_path=ACQUISITION, spectrum_index=8
                )
                structured = loaded["structuredContent"]
                if structured["precursor_mz"] != float(check["observed_mz"]):
                    raise ValueError("Tool precursor differs from independent XML")
                if structured["n_peaks"] != check["n_peaks"] or not math.isclose(
                    structured["tic"], check["tic"], rel_tol=1e-14
                ):
                    raise ValueError(
                        "Decoded peaks/TIC disagree with independent decoder"
                    )
                if (
                    structured["provenance"]["sources"][0]["digest"]
                    != "sha256:" + ACQUISITION_SHA
                ):
                    raise ValueError("Tool provenance digest differs")
                await call("summarise_reference", reference=structured["reference"])
                await call(
                    "annotate_isotopes", identifier="C6H18O3Si3", isotopologues=3
                )
                await call("predict_adduct_offset", adduct_string="[M+H]+")
                validated = await call(
                    "validate_precursor",
                    theoretical_mass=float(check["expected_mz"]),
                    experimental_mass=structured["precursor_mz"],
                    instrument_class="orbitrap",
                )
                if "VALIDATION PASSED" not in json.dumps(validated):
                    raise ValueError(
                        "Tool verdict differs from independent calculation"
                    )
                # Separate measured-spectrum self-match: transport/format smoke
                # check only. It cannot measure identification performance.
                library_query = await call(
                    "load_spectrum", file_path="favipiravir.mgf", spectrum_index=0
                )
                dispatched = await call(
                    "search_library",
                    spectrum_reference=library_query["structuredContent"]["reference"],
                    database_file="favipiravir.msp",
                    scoring_method="classical",
                    instrument_class="tof",
                )
                dispatch_text = json.dumps(dispatched)
                job_match = re.search(r"[0-9a-f]{32}", dispatch_text)
                if job_match is None:
                    raise ValueError("Dispatch returned no job ID")
                deadline = time.monotonic() + 120
                while True:
                    status = await call("check_search_status", job_id=job_match[0])
                    status_text = json.dumps(status)
                    if "Spectral Library Search Results" in status_text:
                        if (
                            "REAL LIBRARY" not in status_text
                            or "1.0000" not in status_text
                        ):
                            raise ValueError(
                                "Measured self-match did not use the real MSP"
                            )
                        break
                    if "FAILED" in status_text.upper() or time.monotonic() > deadline:
                        raise RuntimeError("Search failed or exceeded 120 seconds")
                    await asyncio.sleep(0.25)
                await call("release_reference", reference=structured["reference"])
                await call(
                    "release_reference",
                    reference=library_query["structuredContent"]["reference"],
                )
    revision = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
    ).strip()
    record = {
        "recorded_at_utc": datetime.now(UTC).isoformat(),
        "kind": "Actual MCP SDK replay; no LLM response generated by this script",
        "git_revision": revision,
        "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "uv_lock_sha256": hashlib.sha256((ROOT / "uv.lock").read_bytes()).hexdigest(),
        "server_initialize": initialized.model_dump(
            mode="json", by_alias=True, exclude_none=True
        ),
        "configuration": parameters.env,
        "independent_check": check,
        "calls": entries,
        "path_redaction": "Local data/repo absolute roots replaced by <DATA_DIR>/<REPO_DIR>; all numeric values and tool text otherwise retained.",
    }
    rendered = json.dumps(record, indent=2, ensure_ascii=False)
    for original, replacement in [
        (str(directory), "<DATA_DIR>"),
        (str(ROOT), "<REPO_DIR>"),
    ]:
        rendered = rendered.replace(original, replacement)
        log = output / "server.stderr.txt"
        log.write_text(log.read_text().replace(original, replacement))
    (output / "transcript.json").write_text(rendered + "\n")
    print(json.dumps(check, indent=2))
    print(f"Saved verified MCP replay in {output}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=["prepare", "replay"])
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    directory = args.data_dir.expanduser().resolve()
    if args.operation == "prepare":
        prepare(directory)
    else:
        if args.output is None:
            parser.error("replay requires --output (a new directory)")
        asyncio.run(replay(directory, args.output.expanduser().resolve()))


if __name__ == "__main__":
    main()
