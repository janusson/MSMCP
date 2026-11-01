"""Unvalidated decoys must not turn similarity candidates into discoveries."""

from pathlib import Path

import pytest

from msmcp import security
from msmcp.security import SecurityPolicy
from msmcp.tools import search


@pytest.mark.parametrize("size", [0, 2, 1999, 2000, 2001])
@pytest.mark.parametrize("intensities", [(10, 20, 30), (10, 10, 10)])
def test_uncalibrated_search_ranks_candidates_without_significance_claims(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    size: int,
    intensities: tuple[int, int, int],
) -> None:
    path = tmp_path / "library.msp"
    path.write_text(
        "\n".join(
            f"Name: Candidate-{i}\nPrecursorMZ: 300\nNum Peaks: 3\n"
            f"100 {intensities[0]}\n150 {intensities[1]}\n200 {intensities[2]}\n"
            for i in range(size)
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        security, "DEFAULT_POLICY", SecurityPolicy(allowed_root=tmp_path)
    )

    def forbidden_decoy(*args: object, **kwargs: object) -> None:
        pytest.fail("Production search must not sample an unvalidated decoy null")

    monkeypatch.setattr(search, "_decoy_spectrum", forbidden_decoy)
    outcome = search._run_scan(
        search.SearchRequest(
            database_file=str(path),
            experimental_peaks=((100.0, 10.0), (150.0, 20.0), (200.0, 30.0)),
            experimental_file="known-query.mzML",
        )
    )
    assert outcome.summary.mode == "uncalibrated similarity ranking"
    assert outcome.summary.threshold is None
    assert outcome.summary.top_significance is None
    assert outcome.summary.n_hits == size
    assert "SIGNIFICANCE UNCALIBRATED" in outcome.report
    assert "| FDR (q-value) |" not in outcome.report
    assert "| p-value" not in outcome.report
    assert "passed the threshold" not in outcome.report
    parameters = outcome.provenance.to_dict()["parameters"]
    assert parameters["significance_status"] == "uncalibrated"
    assert parameters["significance_threshold"] is None
    assert parameters["null_decoy_scores"] == 0
    assert parameters["multiple_testing_method"] is None
    assert parameters["fdr_control_validated"] is False
    assert parameters["significance_filter_applied"] is False
    assert parameters["search_result_contract"] == "msmcp.search.similarity.v1"
    assert "null_shape_xi" not in parameters
    assert "null_smallest_attainable_q" not in parameters
    rows = [line for line in outcome.report.splitlines() if line.startswith("| ")]
    assert len(rows) == min(size, 20) + (1 if size else 0)
    if size:
        assert outcome.report.index("SIGNIFICANCE UNCALIBRATED") < outcome.report.index(
            "| Rank |"
        )
    digest = search._digest_lines("job-id", outcome.summary)
    assert "SIGNIFICANCE UNCALIBRATED" in digest
    assert "q =" not in digest
    assert "p =" not in digest


def test_candidates_are_ordered_by_similarity_including_zero_scores(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "ranked.msp"
    path.write_text(
        "Name: Unrelated\nNum Peaks: 2\n50 10\n75 20\n\n"
        "Name: Exact\nNum Peaks: 2\n100 10\n150 20\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        security, "DEFAULT_POLICY", SecurityPolicy(allowed_root=tmp_path)
    )
    outcome = search._run_scan(
        search.SearchRequest(
            database_file=str(path),
            experimental_peaks=((100.0, 10.0), (150.0, 20.0)),
            experimental_file="known-query.mzML",
        )
    )
    assert outcome.summary.top_compound == "Exact"
    assert outcome.summary.top_score == pytest.approx(1.0)
    assert outcome.summary.n_hits == 2
    assert "| 1 | Exact | 1.0000 |" in outcome.report
    assert "| 2 | Unrelated | 0.0000 |" in outcome.report


def test_real_mgf_and_synthetic_fallback_share_the_safety_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "library.mgf"
    path.write_text(
        "BEGIN IONS\nTITLE=Reference\nPEPMASS=300\n100 10\n150 20\nEND IONS\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        security, "DEFAULT_POLICY", SecurityPolicy(allowed_root=tmp_path)
    )
    monkeypatch.setattr(search, "_library_spec", lambda _: (25, 7))
    for library_path, expected_size, synthetic in (
        (str(path), 1, False),
        ("demonstration.unsupported", 25, True),
    ):
        outcome = search._run_scan(
            search.SearchRequest(
                database_file=library_path,
                experimental_peaks=((100.0, 10.0), (150.0, 20.0)),
                experimental_file="known-query.mzML",
            )
        )
        assert outcome.summary.library_synthetic is synthetic
        assert outcome.summary.n_hits == expected_size
        assert outcome.summary.threshold is None
        assert outcome.summary.top_significance is None
        assert "SIGNIFICANCE UNCALIBRATED" in outcome.report
        assert "SIGNIFICANCE UNCALIBRATED" in search._digest_lines(
            "job-id", outcome.summary
        )
        assert (
            outcome.provenance.to_dict()["parameters"]["search_result_contract"]
            == "msmcp.search.similarity.v1"
        )
