"""Unit tests for the opt-in similarity-validation harness (`validation/`).

The harness itself needs `matchms` and an external MassBank dataset, so it is
never run in the fast suite. Its **protocol** logic is pure, though, and it
decides the things the experiment's conclusions rest on: which records are read,
how queries and the database are split, that leakage is prevented, what counts as
an ambiguous match, and how missing data is handled. Those are tested here —
without `matchms` and without any dataset.
"""

from __future__ import annotations

import hashlib
import importlib.util
import random
import sys
import tomllib
from pathlib import Path
from typing import Any

import numpy as np
import pytest


@pytest.fixture(scope="module")
def harness() -> Any:
    """Load ``validation/similarity_validation.py`` by path (it is not a package)."""
    path = (
        Path(__file__).resolve().parents[1] / "validation" / "similarity_validation.py"
    )
    spec = importlib.util.spec_from_file_location("_sim_validation_harness", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses need the module registered
    spec.loader.exec_module(module)
    return module


def test_sdist_excludes_local_cache_builds_and_credentials() -> None:
    project = Path(__file__).resolve().parents[1]
    config = tomllib.loads((project / "pyproject.toml").read_text(encoding="utf-8"))
    excludes = set(config["tool"]["hatch"]["build"]["targets"]["sdist"]["exclude"])
    assert {
        "/.cache/**",
        "/.worktrees/**",
        "/.venv/**",
        "/dist/**",
        "/docs/release-qualification/**",
        "/validation/release_qualification_client.py",
        "/validation/release_performance_probe.py",
        "/.env",
        "/.env.*",
        "/.pypirc",
    } <= excludes


# ---------------------------------------------------------------------------
# load_records — the documented preprocessing filters
# ---------------------------------------------------------------------------
_PEAKS_6 = "\n".join(f"{100 + 10 * i} {10 * (i + 1)}" for i in range(6))
_PEAKS_4 = "\n".join(f"{100 + 10 * i} {10 * (i + 1)}" for i in range(4))


def _msp_record(**fields: str) -> str:
    header = "\n".join(f"{key}: {value}" for key, value in fields.items())
    return f"{header}\n"


_GOOD = {
    "Name": "Good",
    "InChIKey": "KEYGOOD",
    "Ion_mode": "P",
    "Spectrum_type": "MS2",
    "PrecursorMZ": "200",
    "Num Peaks": "6",
}


def _library(tmp_path: Path) -> Path:
    blocks = [
        ("Good", _GOOD, _PEAKS_6),
        (
            "Negative",
            {**_GOOD, "Name": "Negative", "InChIKey": "KEYNEG", "Ion_mode": "N"},
            _PEAKS_6,
        ),
        (
            "LowPrecursor",
            {**_GOOD, "Name": "Low", "InChIKey": "KEYLOW", "PrecursorMZ": "50"},
            _PEAKS_6,
        ),
        (
            "FewPeaks",
            {**_GOOD, "Name": "Few", "InChIKey": "KEYFEW", "Num Peaks": "4"},
            _PEAKS_4,
        ),
        (
            "NoKey",
            {k: v for k, v in _GOOD.items() if k != "InChIKey"} | {"Name": "NoKey"},
            _PEAKS_6,
        ),
        (
            "MS3",
            {**_GOOD, "Name": "MS3", "InChIKey": "KEYMS3", "Spectrum_type": "MS3"},
            _PEAKS_6,
        ),
    ]
    text = "\n\n".join(
        f"# fixture\n{_msp_record(**fields)}{peaks}" for _name, fields, peaks in blocks
    )
    path = tmp_path / "fixture.msp"
    path.write_text(text + "\n", encoding="utf-8")
    return path


def test_load_records_applies_the_documented_filters(
    harness: Any, tmp_path: Path
) -> None:
    """Only positive-mode MS2, InChIKey-bearing, 5-300 peaks, precursor 100-1200."""
    records = harness.load_records(_library(tmp_path))
    assert [r.name for r in records] == ["Good"]
    good = records[0]
    assert good.inchikey == "KEYGOOD"
    assert len(good.peaks) == 6


# ---------------------------------------------------------------------------
# build_sets — the held-out split and its leakage guard
# ---------------------------------------------------------------------------
def _record(
    harness: Any, uid: str, key: str, peaks: tuple[tuple[float, float], ...]
) -> Any:
    return harness.Record(uid=uid, inchikey=key, name=uid, peaks=peaks)


def _synthetic(harness: Any) -> list[Any]:
    """Compound A (3 records), B (2 records), C (1 record -> not eligible)."""
    return [
        _record(harness, "A1", "KEY-A", ((100.0, 10.0), (200.0, 20.0), (300.0, 30.0))),
        _record(harness, "A2", "KEY-A", ((100.0, 10.0), (200.0, 20.0))),
        _record(harness, "A3", "KEY-A", ((150.0, 5.0), (250.0, 7.0))),
        _record(harness, "B1", "KEY-B", ((400.0, 1.0), (500.0, 2.0))),
        _record(harness, "B2", "KEY-B", ((400.0, 1.0), (500.0, 2.0), (600.0, 3.0))),
        _record(harness, "C1", "KEY-C", ((700.0, 1.0), (800.0, 2.0))),
    ]


def test_build_sets_chooses_the_largest_record_as_the_query(harness: Any) -> None:
    queries, _db, truth = harness.build_sets(_synthetic(harness), random.Random(1))
    by_uid = {q.uid: q for q in queries}
    assert set(by_uid) == {"A1", "B2"}  # most peaks per compound; C is ineligible
    assert truth["A1"] == "KEY-A" and truth["B2"] == "KEY-B"


def test_build_sets_keeps_a_genuine_alternative_in_the_database(harness: Any) -> None:
    """A correct answer must require a *different* measurement, not the query itself."""
    queries, database, _truth = harness.build_sets(
        _synthetic(harness), random.Random(1)
    )
    for query in queries:
        same = [r for r in database if r.inchikey == query.inchikey]
        assert same, f"{query.uid} has no alternative record to be recovered"
        assert all(r.peaks != query.peaks for r in database)


def test_build_sets_removes_identical_records_from_the_database(harness: Any) -> None:
    records = _synthetic(harness)
    # Make A3 an exact duplicate of the A1 query: it must not remain as a DB entry.
    records[2] = _record(harness, "A3", "KEY-A", records[0].peaks)
    queries, database, _truth = harness.build_sets(records, random.Random(1))
    query_peaks = {q.peaks for q in queries}
    assert all(r.peaks not in query_peaks for r in database)


def test_build_sets_is_deterministic_for_a_seed(harness: Any) -> None:
    first = harness.build_sets(_synthetic(harness), random.Random(7))
    second = harness.build_sets(_synthetic(harness), random.Random(7))
    assert [r.uid for r in first[0]] == [r.uid for r in second[0]]
    assert [r.uid for r in first[1]] == [r.uid for r in second[1]]


# ---------------------------------------------------------------------------
# _contested — the ambiguity that explains MSMCP/matchms divergence
# ---------------------------------------------------------------------------
def test_contested_when_a_reference_peak_serves_two_query_peaks(harness: Any) -> None:
    query = _record(harness, "Q", "K", ((100.0, 10.0), (100.01, 20.0)))
    reference = _record(harness, "R", "K", ((100.0, 15.0),))
    assert harness._contested(query, reference, 0.02) is True


def test_contested_when_a_query_peak_matches_two_reference_peaks(harness: Any) -> None:
    query = _record(harness, "Q", "K", ((100.0, 10.0),))
    reference = _record(harness, "R", "K", ((100.0, 15.0), (100.01, 5.0)))
    assert harness._contested(query, reference, 0.02) is True


def test_unambiguous_peaks_are_not_contested(harness: Any) -> None:
    query = _record(harness, "Q", "K", ((100.0, 10.0),))
    reference = _record(harness, "R", "K", ((200.0, 5.0),))
    assert harness._contested(query, reference, 0.02) is False


def test_contest_at_the_window_boundary_counts(harness: Any) -> None:
    """A peak exactly at the tolerance is inside the window."""
    query = _record(harness, "Q", "K", ((100.0, 10.0), (100.02, 20.0)))
    reference = _record(harness, "R", "K", ((100.0, 15.0),))
    assert harness._contested(query, reference, 0.02) is True


# ---------------------------------------------------------------------------
# _sha256 and main() missing-data handling (the two do not need matchms)
# ---------------------------------------------------------------------------
def test_ranking_comparison_counts_top1_and_topk_disagreements(harness: Any) -> None:
    database = [
        _record(harness, "A1", "KEY-A", ((100.0, 10.0),)),
        _record(harness, "B1", "KEY-B", ((200.0, 10.0),)),
        _record(harness, "C1", "KEY-C", ((300.0, 10.0),)),
    ]
    queries = [
        _record(harness, "QA", "KEY-A", ((100.0, 10.0),)),
        _record(harness, "QB", "KEY-B", ((200.0, 10.0),)),
    ]
    truth = {"QA": "KEY-A", "QB": "KEY-B"}
    msmcp_matrix = np.asarray([[0.9, 0.8, 0.1], [0.7, 0.6, 0.1]])
    matchms_matrix = np.asarray([[0.8, 0.9, 0.1], [0.1, 0.9, 0.6]])

    result = harness._ranking_comparison(
        msmcp_matrix,
        matchms_matrix,
        database,
        queries,
        truth,
        strict=False,
    )

    assert result["same_top1_reference_record_count"] == 0
    assert result["top1_reference_record_disagreement_count"] == 2
    assert result["top_k"]["1"]["known_compound_hit_outcomes"] == {
        "both_hit": 0,
        "msmcp_only": 1,
        "matchms_only": 1,
        "neither": 0,
    }
    assert result["top_k"]["5"]["known_compound_hit_discordant_queries"] == 0
    assert result["top_k"]["5"]["mean_reference_record_set_jaccard"] == 1.0


def test_sha256_matches_hashlib(harness: Any, tmp_path: Path) -> None:
    payload = tmp_path / "blob.bin"
    payload.write_bytes(b"massbank-placeholder")
    expected = hashlib.sha256(b"massbank-placeholder").hexdigest()
    assert harness._sha256(payload) == expected


def test_main_skips_cleanly_without_a_dataset(
    harness: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("MSMCP_VALIDATION_DATA", raising=False)
    assert harness.main([]) == 0  # absent dataset is a skip, not a failure
    assert "skipped" in capsys.readouterr().err


def test_main_fails_loudly_on_an_explicit_missing_path(
    harness: Any, tmp_path: Path
) -> None:
    missing = tmp_path / "not-there.msp"
    assert harness.main(["--data", str(missing)]) == 2
