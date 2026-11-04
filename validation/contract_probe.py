"""Read-only interoperability probe; runs each project in its own environment.

The MGF input is synthetic. Passing checks characterize current ingestion
contracts; they do not validate compound identification or scientific accuracy.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import tempfile
from pathlib import Path

FIXTURE = """BEGIN IONS
TITLE=valid
PEPMASS=195.08765
RTINSECONDS=120
100 10
150 20
END IONS
BEGIN IONS
TITLE=missing-precursor
RTINSECONDS=180
100 10
150 20
END IONS
BEGIN IONS
TITLE=zero-intensity
PEPMASS=195.08765
RTINSECONDS=240
100 0
150 20
END IONS
"""

MSMCP = """
from pathlib import Path
from msmcp.ingest import resolve_source
from msmcp.security import SecurityPolicy
source = resolve_source(sys.argv[1], SecurityPolicy(allowed_root=Path(sys.argv[1]).parent))
rows = [dict(index=s.index, retention_time=s.retention_time,
             precursor_mz=s.precursor_mz, mz=s.mz.tolist(),
             intensity=s.intensity.tolist(), dtype=str(s.mz.dtype),
             arrays_writeable=bool(s.mz.flags.writeable or s.intensity.flags.writeable))
        for s in source.iter_spectra()]
result = dict(backend=source.backend, rows=rows, module_path=str(Path(sys.modules['msmcp.ingest'].__file__).relative_to(sys.argv[2])))
"""

MASSFLOW = """
from pathlib import Path
from MassFlow.io import load_spectra
rejections = []
rows = [dict(retention_time=s.get('retention_time'),
             precursor_mz=s.get('precursor_mz'), mz=s.peaks.mz.tolist(),
             intensity=s.peaks.intensities.tolist(), dtype=str(s.peaks.mz.dtype))
        for s in load_spectra(Path(sys.argv[1]), rejection_reporter=rejections.append)]
result = dict(backend='MassFlow.io.load_spectra', rows=rows, rejections=rejections, module_path=str(Path(sys.modules['MassFlow.io'].__file__).relative_to(sys.argv[2])))
"""


def probe(root: Path, code: str, fixture: Path, scratch: Path) -> dict:
    process = subprocess.run(
        [
            str(root / ".venv/bin/python"),
            "-c",
            "import json,sys,platform,importlib.metadata\n"
            + code
            + "\nresult['runtime'] = {'python': platform.python_version(), 'packages': {}}\n"
            + "for name in ('msmcp', 'massflow', 'numpy', 'matchms'):\n"
            + "    try: result['runtime']['packages'][name] = importlib.metadata.version(name)\n"
            + "    except importlib.metadata.PackageNotFoundError: pass\n"
            + "print('CONTRACT_JSON='+json.dumps(result))",
            str(fixture),
            str(root),
        ],
        cwd=scratch,
        env={**os.environ, "PYTHONPATH": str(root / "src")},
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    marker = "CONTRACT_JSON="
    records = [line for line in process.stdout.splitlines() if line.startswith(marker)]
    if len(records) != 1:
        raise RuntimeError("Expected exactly one structured probe result")
    result = json.loads(records[0][len(marker) :])
    result["stdout_other_lines"] = sum(
        not line.startswith(marker) for line in process.stdout.splitlines()
    )
    result["source_fingerprints"] = {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted((root / "src").rglob("*.py"))
    }
    return result


def evaluate_contract(msmcp: dict, massflow: dict) -> dict[str, bool]:
    """Report every known invariant, including absent/empty reader results.

    This checks a frozen synthetic fixture, not general equivalence or scientific
    validity. Inputs are never changed; drift produces false checks, not a pass.
    """
    left = msmcp.get("rows", [])
    right = massflow.get("rows", [])
    first_left = left[0] if left else {}
    first_right = right[0] if right else {}
    return {
        "msmcp_preserves_three_records": len(left) == 3,
        "massflow_reports_two_rejections": len(right) == 1
        and massflow.get("rejections")
        == ["Missing precursor_mz", "Contains non-positive intensity values"],
        "shared_valid_peaks_unchanged": first_left.get("mz")
        == first_right.get("mz")
        == [100.0, 150.0]
        and first_left.get("intensity") == first_right.get("intensity") == [10.0, 20.0]
        and first_left.get("precursor_mz")
        == first_right.get("precursor_mz")
        == 195.08765,
        "retention_units_require_conversion": first_left.get("retention_time") == 2.0
        and first_right.get("retention_time") == 120.0,
        "precursor_not_invented": len(left) == 3
        and "precursor_mz" in left[1]
        and left[1]["precursor_mz"] is None,
        "zero_intensity_not_erased": len(left) == 3
        and left[2].get("intensity") == [0.0, 20.0],
        "float64_arrays": bool(left)
        and bool(right)
        and all(row.get("dtype") == "float64" for row in left + right),
        "msmcp_arrays_read_only": bool(left)
        and all(row.get("arrays_writeable") is False for row in left),
        "stdout_clean_in_this_probe": msmcp.get("stdout_other_lines")
        == massflow.get("stdout_other_lines")
        == 0,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--msmcp-root", required=True, type=Path)
    parser.add_argument("--massflow-root", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="msmcp-contract-") as directory:
        scratch = Path(directory)
        fixture = scratch / "contract.mgf"
        fixture.write_text(FIXTURE, encoding="utf-8")
        msmcp = probe(args.msmcp_root.resolve(), MSMCP, fixture, scratch)
        massflow = probe(args.massflow_root.resolve(), MASSFLOW, fixture, scratch)
    checks = evaluate_contract(msmcp, massflow)
    output = {
        "input": "synthetic MGF, three records; not an identification benchmark",
        "fixture_sha256": hashlib.sha256(FIXTURE.encode()).hexdigest(),
        "msmcp": msmcp,
        "MassFlow": massflow,
        "checks": checks,
        "all_checks_passed": all(checks.values()),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"checks": checks, "output": str(args.output)}))
    if not output["all_checks_passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
