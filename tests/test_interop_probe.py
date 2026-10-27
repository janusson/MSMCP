"""Hermetic checks of the optional two-environment audit's failure behavior."""

import copy
import runpy
from pathlib import Path

import pytest


def _evaluate(msmcp: dict, massflow: dict) -> dict[str, bool]:
    namespace = runpy.run_path(
        str(Path(__file__).resolve().parents[1] / "validation/contract_probe.py")
    )
    return namespace["evaluate_contract"](msmcp, massflow)


def _results() -> tuple[dict, dict]:
    row = {
        "retention_time": 2.0,
        "precursor_mz": 195.08765,
        "mz": [100.0, 150.0],
        "intensity": [10.0, 20.0],
        "dtype": "float64",
        "arrays_writeable": False,
    }
    absent = {**row, "precursor_mz": None}
    zero = {**row, "intensity": [0.0, 20.0]}
    return (
        {"rows": [row, absent, zero], "stdout_other_lines": 0},
        {
            "rows": [{**row, "retention_time": 120.0}],
            "rejections": [
                "Missing precursor_mz",
                "Contains non-positive intensity values",
            ],
            "stdout_other_lines": 0,
        },
    )


def test_expected_contract_passes_without_installing_either_environment() -> None:
    assert all(_evaluate(*_results()).values())


@pytest.mark.parametrize("side", [0, 1])
def test_empty_reader_output_fails_instead_of_crashing(side: int) -> None:
    values = list(_results())
    values[side]["rows"] = []
    assert not all(_evaluate(*values).values())


@pytest.mark.parametrize(
    ("field", "value"),
    [("retention_time", 120.0), ("dtype", "float32"), ("arrays_writeable", True)],
)
def test_drift_is_detected_without_mutating_results(field: str, value: object) -> None:
    msmcp, massflow = _results()
    msmcp["rows"][0][field] = value
    before = copy.deepcopy((msmcp, massflow))
    assert not all(_evaluate(msmcp, massflow).values())
    assert (msmcp, massflow) == before


def test_stdout_contamination_is_a_failed_check() -> None:
    msmcp, massflow = _results()
    massflow["stdout_other_lines"] = 1
    assert not _evaluate(msmcp, massflow)["stdout_clean_in_this_probe"]


def test_probe_observes_child_stdout_and_fingerprints_sources(tmp_path: Path) -> None:
    import hashlib
    import sys

    root = tmp_path / "project with spaces"
    (root / ".venv/bin").mkdir(parents=True)
    (root / ".venv/bin/python").symlink_to(sys.executable)
    (root / "src").mkdir()
    source = root / "src/io.py"
    source.write_text("# synthetic probe source\n")
    fixture = tmp_path / "fixture.mgf"
    fixture.write_text("synthetic fixture\n")
    namespace = runpy.run_path(
        str(Path(__file__).resolve().parents[1] / "validation/contract_probe.py")
    )
    result = namespace["probe"](
        root, "print('diagnostic')\nresult = {'rows': []}", fixture, tmp_path
    )
    assert result["rows"] == []
    assert result["stdout_other_lines"] == 1
    assert (
        result["source_fingerprints"]["src/io.py"]
        == hashlib.sha256(source.read_bytes()).hexdigest()
    )
    assert fixture.read_text() == "synthetic fixture\n"


def test_duplicate_structured_child_results_are_rejected(tmp_path: Path) -> None:
    import sys

    root = tmp_path / "project"
    (root / ".venv/bin").mkdir(parents=True)
    (root / ".venv/bin/python").symlink_to(sys.executable)
    fixture = tmp_path / "fixture.mgf"
    fixture.write_text("synthetic fixture\n")
    namespace = runpy.run_path(
        str(Path(__file__).resolve().parents[1] / "validation/contract_probe.py")
    )
    with pytest.raises(RuntimeError, match="exactly one structured"):
        namespace["probe"](
            root, "print('CONTRACT_JSON={}')\nresult = {}", fixture, tmp_path
        )
