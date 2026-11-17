"""Offline deterministic smoke test for ``examples/reproducible_workflows.py``.

Runs all three workflows (exact-mass chemistry, synthetic caffeine library
search, and synthetic LC-MS exploration) over the real stdio MCP server without
modifying tracked repository files or performing network requests.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_SCRIPT = REPO_ROOT / "examples" / "reproducible_workflows.py"


def _load_examples_module() -> Any:
    spec = importlib.util.spec_from_file_location(
        "msmcp_examples_reproducible_workflows", WORKFLOW_SCRIPT
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def test_reproducible_workflows_offline_smoke(tmp_path: Path) -> None:
    """All three example workflows execute offline with deterministic outputs."""
    mod = _load_examples_module()
    results = await mod.run_workflows(
        tmp_path,
        server_cmd=[sys.executable, "-m", "msmcp.server"],
        examples_dir=REPO_ROOT / "examples",
    )

    assert set(results["tools"]) == mod.EXPECTED_TOOLS
    assert len(results["mzml_sha256"]) == 64

    # Workflow 1: exact-mass chemistry & Orbitrap precursor tolerance gate
    chem = results["chemistry"]
    assert "Monoisotopic mass: **194.0804 Da**" in chem["isotopes"]
    for abundance in ("1.0000", "0.1032", "0.0089", "0.0006"):
        assert abundance in chem["isotopes"]
    assert "Exact mass shift (Δ): +1.007276 Da" in chem["adduct"]
    assert "Charge state: +1" in chem["adduct"]
    assert "VALIDATION PASSED" in chem["mass_pass"]
    assert "0.12 ppm" in chem["mass_pass"]
    assert "VALIDATION REJECTED" in chem["mass_reject"]
    assert "11.91 ppm" in chem["mass_reject"]

    # Workflow 2: synthetic caffeine library search & pairwise cosine
    lib = results["library"]
    assert lib["loaded"]["reference"].startswith("ptr:spectrum:")
    assert lib["loaded"]["ms_level"] == 2
    assert lib["loaded"]["precursor_mz"] == pytest.approx(195.0877)
    assert lib["loaded"]["n_peaks"] == 4
    report = lib["search_report"]
    assert "REAL LIBRARY — READ FROM DISK" in report
    assert "SYNTHETIC LIBRARY" not in report
    assert "SIGNIFICANCE UNCALIBRATED" in report
    assert (
        "1 matched, 0 unknown, 4 excluded before ranking "
        "(reasons: precursor outside declared tolerance (4))"
    ) in report
    assert (
        "| 1 | Caffeine | 1.0000 | 195.0877 | C8H10N4O2 | positive | matched |"
        in report
    )
    assert "Cosine Similarity: **1.0000**" in lib["cosine"]
    assert "4 / 4 query peaks" in lib["cosine"]
    assert "Released" in lib["released"]

    # Workflow 3: synthetic LC-MS inspection, chromatograms, and QC summary
    lcms = results["lcms"]
    assert "45 of 45 spectra" in lcms["spectra"]
    chrom = lcms["chromatograms"]
    assert chrom["schema_version"] == "1.0"
    assert chrom["acquisition"]["spectrum_counts_by_ms_level"] == {"1": 41, "2": 4}
    assert chrom["completeness"]["full_acquisition_processed"] is True
    times = chrom["chromatograms"]["retention_times_min"]
    assert len(times) == 41
    assert times[0] == pytest.approx(0.0)
    assert times[-1] == pytest.approx(4.0)
    xic_195, xic_250 = chrom["chromatograms"]["xic"]
    assert xic_195["intensities"][15] == pytest.approx(1000.0)
    assert xic_250["intensities"][27] == pytest.approx(650.0)
    assert "**Spectra analysed:** 45" in lcms["qc"]
    assert "2.9" in lcms["qc"]
