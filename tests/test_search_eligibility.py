"""Context eligibility for library search (issue #79).

Library search is only meaningful when the query is MS2 evidence and the
reference record is compatible with the acquisition context that produced it.
These tests pin the bounded correction:

* an MS1 (or non-MS2) query is refused rather than scored as an MS/MS match;
* a reference whose stated precursor is outside the declared tolerance, or
  whose polarity/charge/adduct conflicts with the query, is excluded before
  ranking;
* a reference that omits context stays ranked but is labelled *unknown*;
* a direct peak-list query has no acquisition context and is reported as
  fragment-only exploratory; the precursor tolerance is not claimed as applied.

These are software-contract tests.  They do not establish analytical
identification accuracy or a calibrated false-discovery rate.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from msmcp import security
from msmcp.instruments import get_profile
from msmcp.security import SecurityPolicy
from msmcp.tools.search import (
    QueryContext,
    SearchRequest,
    _assess_eligibility,
    _run_scan,
)

QUERY_PEAKS: list[tuple[float, float]] = [
    (110.0713, 40.0),
    (120.0808, 100.0),
    (136.0757, 60.0),
    (138.0662, 420.0),
]

# The 900 entry has exactly the query's peak list.  Without context gating it
# ranks first at score 1.0 even though its precursor cannot be the query's.
ELIGIBILITY_MSP = """\
Name: Correct precursor
Formula: C8H10N4O2
PrecursorMZ: 100.0000
Ion_mode: P
Num Peaks: 4
110.0713 40.0
120.0808 100.0
136.0757 60.0
138.0662 420.0

Name: Wrong precursor
Formula: C8H10N4O2
PrecursorMZ: 900.0000
Ion_mode: P
Num Peaks: 4
110.0713 40.0
120.0808 100.0
136.0757 60.0
138.0662 420.0

Name: Wrong polarity
Formula: C8H10N4O2
PrecursorMZ: 100.0000
Ion_mode: N
Num Peaks: 4
110.0713 40.0
120.0808 100.0
136.0757 60.0
138.0662 420.0

Name: No precursor stated
Formula: C8H10N4O2
Ion_mode: P
Num Peaks: 4
110.0713 40.0
120.0808 100.0
136.0757 60.0
138.0662 420.0
"""


def _write_library(tmp_path: Path) -> Path:
    path = tmp_path / "eligibility.msp"
    path.write_text(ELIGIBILITY_MSP, encoding="utf-8")
    return path


def _run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    context: QueryContext | None,
) -> tuple[str, dict[str, object]]:
    monkeypatch.setattr(
        security, "DEFAULT_POLICY", SecurityPolicy(allowed_root=tmp_path)
    )
    outcome = _run_scan(
        SearchRequest(
            database_file=str(_write_library(tmp_path)),
            experimental_peaks=tuple(QUERY_PEAKS),
            experimental_file="query.mgf",
            query_context=context,
            query_polarity=context.polarity if context is not None else None,
        )
    )
    return outcome.report, outcome.provenance.to_dict()["parameters"]


class TestQueryLevelGate:
    def test_ms1_query_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        with pytest.raises(Exception, match="MS1 survey scan"):
            _run(
                tmp_path,
                monkeypatch,
                QueryContext(ms_level=1, precursor_mz=100.0, source="file"),
            )

    def test_ms3_query_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        with pytest.raises(Exception, match="MS3"):
            _run(
                tmp_path,
                monkeypatch,
                QueryContext(ms_level=3, precursor_mz=100.0, source="file"),
            )


class TestReferenceEligibility:
    def test_incompatible_precursor_is_excluded_before_ranking(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        report, parameters = _run(
            tmp_path,
            monkeypatch,
            QueryContext(
                ms_level=2,
                precursor_mz=100.0,
                polarity="positive",
                source="file",
            ),
        )

        assert "Wrong precursor" not in report
        assert "| 1 | Correct precursor |" in report
        assert "precursor outside declared tolerance (1)" in report
        assert parameters["precursor_tolerance_applied"] is True
        assert parameters["precursor_tolerance_used"] == {"value": 5.0, "unit": "ppm"}
        assert parameters["eligibility_counts"] == {
            "matched": 1,
            "unknown": 1,
            "excluded": 2,
        }

    def test_incompatible_polarity_is_excluded(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        report, parameters = _run(
            tmp_path,
            monkeypatch,
            QueryContext(
                ms_level=2,
                precursor_mz=100.0,
                polarity="positive",
                source="file",
            ),
        )

        assert "Wrong polarity" not in report
        assert "polarity mismatch (1)" in report
        assert parameters["eligibility_exclusion_reasons"] == {
            "precursor outside declared tolerance": 1,
            "polarity mismatch": 1,
        }

    def test_unknown_reference_context_stays_ranked_but_is_labelled(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        report, _ = _run(
            tmp_path,
            monkeypatch,
            QueryContext(
                ms_level=2,
                precursor_mz=100.0,
                polarity="positive",
                source="file",
            ),
        )

        assert "No precursor stated" in report
        assert "unknown: reference precursor m/z not stated" in report
        assert "1 matched, 1 unknown, 2 excluded" in report

    def test_precursor_within_tolerance_is_matched(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # 100.0003 is 3 ppm from 100.0; the generic profile allows 5 ppm.
        library = ELIGIBILITY_MSP.replace(
            "PrecursorMZ: 100.0000", "PrecursorMZ: 100.0003", 1
        )
        path = tmp_path / "within.msp"
        path.write_text(library, encoding="utf-8")
        monkeypatch.setattr(
            security, "DEFAULT_POLICY", SecurityPolicy(allowed_root=tmp_path)
        )
        outcome = _run_scan(
            SearchRequest(
                database_file=str(path),
                experimental_peaks=tuple(QUERY_PEAKS),
                experimental_file="query.mgf",
                query_context=QueryContext(
                    ms_level=2,
                    precursor_mz=100.0,
                    polarity="positive",
                    source="file",
                ),
            )
        )

        # Only the 900 entry is outside tolerance; the near-match stays eligible.
        assert "precursor outside declared tolerance (1)" in outcome.report
        assert "| 1 | Correct precursor |" in outcome.report
        assert "| matched |" in outcome.report


class TestUnknownQueryContext:
    def test_direct_peak_list_is_exploratory_and_tolerance_is_not_claimed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        report, parameters = _run(tmp_path, monkeypatch, None)

        assert "Query context: unavailable" in report
        assert "fragment-only exploratory" in report
        assert "Context gate: precursor tolerance not applied" in report
        assert parameters["precursor_tolerance_applied"] is False
        assert parameters["precursor_tolerance_used"] is None
        # Every candidate remains ranked; none is promoted to context-matched.
        assert parameters["eligibility_counts"] == {
            "matched": 0,
            "unknown": 4,
            "excluded": 0,
        }


class TestAdductAndCharge:
    def test_adduct_mismatch_is_excluded(self) -> None:
        profile = get_profile(None)
        result = _assess_eligibility(
            QueryContext(
                ms_level=2,
                precursor_mz=100.0,
                polarity="positive",
                adduct="[M+H]+",
                source="file",
            ),
            {
                "precursor_mz": 100.0,
                "polarity": "positive",
                "metadata": {"Precursor_type": "[M+Na]+"},
            },
            profile,
        )

        assert result["status"] == "excluded"
        assert "adduct mismatch" in str(result["reason"])

    def test_charge_mismatch_is_excluded(self) -> None:
        profile = get_profile(None)
        result = _assess_eligibility(
            QueryContext(
                ms_level=2,
                precursor_mz=100.0,
                polarity="positive",
                charge=2,
                source="file",
            ),
            {
                "precursor_mz": 100.0,
                "polarity": "positive",
                "metadata": {"Charge": "1"},
            },
            profile,
        )

        assert result["status"] == "excluded"
        assert "charge mismatch" in str(result["reason"])
