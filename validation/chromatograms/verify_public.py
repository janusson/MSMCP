"""Opt-in public-data comparison: independent stdlib decoder vs real MCP stdio.

No MSMCP reader/calculator imports. Inputs must already match the pinned manifest.
No downloads, data changes, or MassPilot installation are performed.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import importlib.metadata
import json
import math
import os
import platform
import struct
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
import zlib
from pathlib import Path
from typing import Any

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

import msmcp

TARGETS = [195.0877, 223.0634, 297.0822, 299.0614]
NAMES = ["Orbi_P_E2_CSSM10uMA.mzML", "Orbi_P_E2_EXBLANKA.mzML"]


def sha256(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def reference(path: Path) -> dict[str, Any]:
    """Decode CV-described IEEE floats directly; sum with math.fsum, not numpy.

    This intentionally supports only the compression/precision/RT units present
    in this fixed public example, and refuses to guess any others.
    """
    rows = []
    scanned = 0
    declared = None
    for event, element in ET.iterparse(path, events=("start", "end")):
        tag = element.tag.rsplit("}", 1)[-1]
        if event == "start" and tag == "spectrumList":
            declared = int(element.attrib["count"])
        if event != "end" or tag != "spectrum":
            continue
        ordinal = scanned
        scanned += 1
        cv = {c.attrib["accession"]: c for c in element if c.tag.endswith("cvParam")}
        if int(cv["MS:1000511"].attrib["value"]) != 1:
            element.clear()
            continue
        assert "MS:1000127" in cv, "Example changed: expected centroid MS1"
        times = [
            c
            for c in element.iter()
            if c.tag.endswith("cvParam") and c.get("accession") == "MS:1000016"
        ]
        assert len(times) == 1
        rt = float(times[0].attrib["value"])
        unit = times[0].attrib["unitAccession"]
        if unit == "UO:0000010":
            rt /= 60
        else:
            assert unit == "UO:0000031", f"Unexpected RT unit {unit}"
        arrays = {}
        for array in element.iter():
            if not array.tag.endswith("}binaryDataArray"):
                continue
            terms = {c.get("accession") for c in array if c.tag.endswith("cvParam")}
            kind = "mz" if "MS:1000514" in terms else "intensity"
            assert {"MS:1000514", "MS:1000515"} & terms
            assert not {"MS:1002312", "MS:1002313", "MS:1002314"} & terms
            precision = "d" if "MS:1000523" in terms else "f"
            assert {"MS:1000523", "MS:1000521"} & terms
            binary = next(c for c in array if c.tag.endswith("}binary"))
            data = base64.b64decode("".join((binary.text or "").split()), validate=True)
            if "MS:1000574" in terms:
                data = zlib.decompress(data)
            else:
                assert "MS:1000576" in terms
            size = struct.calcsize(precision)
            assert len(data) % size == 0
            arrays[kind] = [v[0] for v in struct.iter_unpack("<" + precision, data)]
        mz, intensity = arrays["mz"], arrays["intensity"]
        assert len(mz) == len(intensity) == int(element.attrib["defaultArrayLength"])
        assert all(math.isfinite(v) for v in [*mz, *intensity])
        xic = [
            math.fsum(
                i
                for m, i in zip(mz, intensity, strict=True)
                if target - target * 5 / 1_000_000
                <= m
                <= target + target * 5 / 1_000_000
            )
            for target in TARGETS
        ]
        rows.append(
            dict(
                ordinal=ordinal,
                index=int(element.attrib["index"]),
                native_id=element.attrib["id"],
                rt=rt,
                tic=math.fsum(intensity),
                bpc=max(intensity) if intensity else None,
                xic=xic,
            )
        )
        element.clear()
    assert scanned == declared
    return dict(scanned=scanned, rows=rows)


def require_schema(payload: dict[str, Any]) -> None:
    if payload.get("schema_version") != "1.0":
        raise ValueError("Unsupported chromatogram schema revision")


def compare(payload: dict[str, Any], ref: dict[str, Any]) -> dict[str, Any]:
    require_schema(payload)
    trace = payload["chromatograms"]
    rows = ref["rows"]
    count = len(rows)
    assert payload["completeness"]["full_acquisition_processed"] is True
    assert payload["acquisition"]["scanned_spectra"] == ref["scanned"]
    assert payload["completeness"]["returned_points"] == count
    assert payload["missing_observations"] == []
    assert trace["retention_time_units"] == "minutes"
    assert trace["intensity_units"] == "input_defined"
    for key, ref_key in [
        ("scan_ordinals", "ordinal"),
        ("source_spectrum_indices", "index"),
        ("native_scan_ids", "native_id"),
    ]:
        assert trace[key] == [row[ref_key] for row in rows]
    vectors = [
        ("RT_min", trace["retention_times_min"], [r["rt"] for r in rows]),
        ("TIC", trace["tic_intensities"], [r["tic"] for r in rows]),
        ("BPC", trace["bpc_intensities"], [r["bpc"] for r in rows]),
    ]
    for i, target in enumerate(TARGETS):
        assert trace["xic"][i]["target_mz"] == target
        vectors.append(
            (
                f"XIC_{target}",
                trace["xic"][i]["intensities"],
                [r["xic"][i] for r in rows],
            )
        )
    numerical = {}
    for name, actual, expected in vectors:
        assert len(actual) == len(expected) == count
        errors = []
        for a, e in zip(actual, expected, strict=True):
            if e is None:
                assert a is None
            else:
                assert a is not None and math.isclose(
                    a, e, rel_tol=1e-12, abs_tol=1e-8
                ), (name, a, e)
                errors.append(abs(a - e))
        numerical[name] = dict(compared=count, max_abs_error=max(errors, default=0.0))
    representative = []
    for i, target in enumerate(TARGETS):
        peak = max(range(count), key=lambda j: rows[j]["xic"][i])
        row = rows[peak]
        representative.append(
            dict(
                target_mz=target,
                scan_ordinal=row["ordinal"],
                native_id=row["native_id"],
                retention_time_min=row["rt"],
                reference_xic=row["xic"][i],
                mcp_xic=trace["xic"][i]["intensities"][peak],
                reference_tic=row["tic"],
                mcp_tic=trace["tic_intensities"][peak],
                zero_xic_points=sum(r["xic"][i] == 0 for r in rows),
            )
        )
    return dict(
        scanned_spectra=ref["scanned"],
        ms1_points=count,
        numerical=numerical,
        representative_xic_maxima=representative,
        acquisition=payload["acquisition"],
        warnings=payload["warnings"],
        source=payload["source"],
    )


async def verify(data_dir: Path) -> dict[str, Any]:
    checkout = Path(__file__).resolve().parents[2]
    package_path = Path(msmcp.__file__).resolve()
    if package_path != checkout / "src/msmcp/__init__.py":
        raise RuntimeError(f"Wrong MSMCP package under test: {package_path}")
    if Path(sys.prefix).resolve() != (checkout / ".venv").resolve():
        raise RuntimeError("Run through this checkout's uv environment")
    code_paths = subprocess.check_output(
        [
            "git",
            "ls-files",
            "src",
            "uv.lock",
            "pyproject.toml",
            "validation/chromatograms/verify_public.py",
        ],
        text=True,
        cwd=checkout,
    ).splitlines()
    source_hashes = {name: sha256(checkout / name) for name in code_paths}
    manifest = json.loads(Path(__file__).with_name("MANIFEST.json").read_text())
    entries = {e["path"]: e for e in manifest["files"]}
    for name in NAMES:
        path = data_dir / name
        assert path.stat().st_size == entries[name]["bytes"]
        assert sha256(path) == entries[name]["sha256"], f"Input digest mismatch: {name}"
    env = dict(
        os.environ,
        MSMCP_ALLOWED_ROOT=str(data_dir),
        MSMCP_EMBEDDING_BACKEND="mock",
        PYTHONPATH=str(checkout / "src"),
    )
    # Mock embeddings disable unrelated optional model loading; chromatograms
    # always read the actual mzML files and have no synthetic fallback.
    server = StdioServerParameters(
        command=sys.executable, args=["-m", "msmcp.server"], env=env, cwd=str(checkout)
    )
    report: dict[str, Any] = dict(
        dataset_accession=manifest["dataset_accession"],
        manifest_sha256=sha256(Path(__file__).with_name("MANIFEST.json")),
        source_commit=subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True, cwd=checkout
        ).strip(),
        source_files_sha256=source_hashes,
        source_tree_status=subprocess.check_output(
            [
                "git",
                "status",
                "--porcelain",
                "--",
                "src",
                "uv.lock",
                "pyproject.toml",
                "validation/chromatograms/verify_public.py",
            ],
            text=True,
            cwd=checkout,
        ).strip(),
        package_path=str(package_path),
        environment_prefix=sys.prefix,
        python=platform.python_version(),
        packages={
            p: importlib.metadata.version(p)
            for p in ["msmcp", "mcp", "numpy", "pydantic", "massflow"]
        },
        targets_mz=TARGETS,
        tolerance_ppm=5,
        comparison=dict(relative_tolerance=1e-12, absolute_tolerance=1e-8),
        files={},
    )
    async with (
        stdio_client(server) as (read, write),
        ClientSession(read, write, read_timeout_seconds=120) as session,
    ):
        await session.initialize()
        listing = await session.list_tools()
        tool = next(t for t in listing.tools if t.name == "extract_chromatograms")
        assert tool.output_schema is not None
        report["output_schema_sha256"] = hashlib.sha256(
            json.dumps(tool.output_schema, sort_keys=True).encode()
        ).hexdigest()
        for name in NAMES:
            start = time.monotonic()
            response = await session.call_tool(
                "extract_chromatograms",
                dict(
                    file_path=str(data_dir / name),
                    targets_mz=TARGETS,
                    tolerance=5.0,
                    tolerance_unit="ppm",
                    ms_level=1,
                    max_spectra=10000,
                ),
            )
            assert not response.is_error, response
            payload = response.structured_content
            assert isinstance(payload, dict)
            assert payload["source"]["digest"] == "sha256:" + entries[name]["sha256"]
            assert (
                payload["provenance"]["sources"][0]["digest"]
                == payload["source"]["digest"]
            )
            elapsed = time.monotonic() - start
            ref = reference(data_dir / name)
            summary = compare(payload, ref)
            again = await session.call_tool(
                "extract_chromatograms",
                dict(
                    file_path=str(data_dir / name),
                    targets_mz=TARGETS,
                    max_spectra=10000,
                ),
            )
            assert not again.is_error and isinstance(again.structured_content, dict)
            for key in [
                "schema_version",
                "source",
                "acquisition",
                "chromatograms",
                "extraction_parameters",
                "completeness",
                "missing_observations",
                "warnings",
            ]:
                assert payload[key] == again.structured_content[key], key
            summary["repeated_call_equal_except_provenance_timestamp"] = True
            summary["stdio_extraction_seconds"] = elapsed
            report["files"][name] = summary
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = asyncio.run(verify(args.data_dir.resolve()))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, allow_nan=False) + "\n")
    print(f"PASS: independent sample/blank comparisons; report: {args.output}")
