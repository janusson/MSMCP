"""Acquisition-context gates for candidate spectral-library matches."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from types import SimpleNamespace

import pytest

from msmcp import ingest, security
from msmcp.errors import MsmcpError
from msmcp.state import store as reference_store
from msmcp.tools import search
from msmcp.tools.search import SearchRequest, _digest_lines, _run_scan

PEAKS = (
    (110.0713, 40.0),
    (120.0808, 100.0),
    (136.0757, 60.0),
    (138.0662, 420.0),
    (500.1, 55.0),
)


def _library(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "competing.msp"
    records = []
    for name, precursor in (("Incompatible", 900.0), ("Compatible", 195.0877)):
        records.append(
            f"Name: {name}\nPrecursorMZ: {precursor}\nIon_mode: P\n"
            f"Num Peaks: {len(PEAKS)}\n"
            + "".join(f"{mz} {intensity}\n" for mz, intensity in PEAKS)
        )
    path.write_text("\n".join(records), encoding="utf-8")
    monkeypatch.setattr(
        security, "DEFAULT_POLICY", security.SecurityPolicy(allowed_root=tmp_path)
    )
    return path


def test_ms2_precursor_gate_excludes_fragment_identical_incompatible_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    msp = _library(tmp_path, monkeypatch)
    outcome = _run_scan(
        SearchRequest(
            database_file=str(msp),
            experimental_peaks=PEAKS,
            experimental_file="query.mzML",
            query_ms_level=2,
            query_precursor_mz=195.0877,
            query_polarity="positive",
        )
    )
    assert "| 1    | Compatible" in outcome.report
    assert "Incompatible" not in outcome.report
    parameters = outcome.provenance.to_dict()["parameters"]
    assert parameters["precursor_tolerance"] == {"value": 5.0, "unit": "ppm"}
    assert parameters["precursor_candidates_excluded"] == 1


def test_no_query_precursor_is_explicitly_exploratory_and_has_no_applied_tolerance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    msp = _library(tmp_path, monkeypatch)
    outcome = _run_scan(
        SearchRequest(
            database_file=str(msp),
            experimental_peaks=PEAKS,
            experimental_file="query.mzML",
            query_ms_level=2,
        )
    )
    assert "fragment-only exploratory" in outcome.report.lower()
    parameters = outcome.provenance.to_dict()["parameters"]
    assert "precursor_tolerance" not in parameters
    assert parameters["precursor_gate"] == "not applied: query precursor absent"
    assert "fragment-only exploratory" in _digest_lines("job", outcome.summary)


def test_context_qualified_ms2_excludes_opposite_polarity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    msp = _library(tmp_path, monkeypatch)
    outcome = _run_scan(
        SearchRequest(
            database_file=str(msp),
            experimental_peaks=PEAKS,
            experimental_file="negative.mzML",
            query_ms_level=2,
            query_precursor_mz=195.0877,
            query_polarity="negative",
        )
    )
    assert "| 1    | Compatible" not in outcome.report
    parameters = outcome.provenance.to_dict()["parameters"]
    assert parameters["polarity_candidates_excluded"] == 1


def test_precursor_window_uses_instrument_units(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    msp = _library(tmp_path, monkeypatch)
    msp.write_text(
        msp.read_text(encoding="utf-8").replace(
            "PrecursorMZ: 195.0877", "PrecursorMZ: 195.2"
        ),
        encoding="utf-8",
    )
    generic = _run_scan(
        SearchRequest(
            database_file=str(msp),
            experimental_peaks=PEAKS,
            query_ms_level=2,
            query_precursor_mz=195.0877,
            instrument_class="generic",
        )
    )
    ion_trap = _run_scan(
        SearchRequest(
            database_file=str(msp),
            experimental_peaks=PEAKS,
            query_ms_level=2,
            query_precursor_mz=195.0877,
            instrument_class="ion_trap",
        )
    )
    assert generic.provenance.to_dict()["parameters"]["eligible_library_spectra"] == 0
    assert ion_trap.provenance.to_dict()["parameters"]["eligible_library_spectra"] == 1
    assert ion_trap.provenance.to_dict()["parameters"]["precursor_tolerance"] == {
        "value": 0.5,
        "unit": "Da",
    }


def test_invalid_query_precursor_is_refused_instead_of_bypassing_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    msp = _library(tmp_path, monkeypatch)
    with pytest.raises(MsmcpError, match="finite positive precursor"):
        _run_scan(
            SearchRequest(
                database_file=str(msp),
                experimental_peaks=PEAKS,
                query_ms_level=2,
                query_precursor_mz=float("nan"),
            )
        )


@pytest.mark.asyncio
async def test_file_and_reference_queries_carry_ms2_precursor_into_scan(
    search_tools: dict[str, Callable[..., Awaitable[str]]],
    searchable_mzml: Path,
    valid_mgf: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The public dispatcher must not drop acquisition context before scoring."""
    monkeypatch.setattr(
        ingest,
        "DEFAULT_POLICY",
        security.SecurityPolicy(allowed_root=searchable_mzml.parent),
    )
    seen: list[SearchRequest] = []

    def capture(request: SearchRequest) -> None:
        seen.append(request)

    def submit(_name: str, work: Callable[[], object], **_kwargs: object) -> object:
        work()
        return SimpleNamespace(job_id="0" * 32)

    monkeypatch.setattr(search, "_run_scan", capture)
    monkeypatch.setattr(search, "_EXECUTOR", SimpleNamespace(submit=submit))

    await search_tools["search_library"](
        experimental_file=str(searchable_mzml), database_file="library/demo.txt"
    )
    await search_tools["search_library"](
        experimental_file=str(valid_mgf), database_file="library/demo.txt"
    )
    spectrum = next(iter(ingest.resolve_source(searchable_mzml).iter_spectra()))
    reference = reference_store.store_spectrum(spectrum)
    try:
        await search_tools["search_library"](
            spectrum_reference=reference.identifier, database_file="library/demo.txt"
        )
    finally:
        reference_store.release(reference.identifier)

    assert len(seen) == 3
    assert [request.query_ms_level for request in seen] == [2, 2, 2]
    assert [request.query_precursor_mz for request in seen] == [
        300.0,
        194.0804,
        300.0,
    ]


@pytest.mark.asyncio
async def test_ms1_file_query_is_refused_before_dispatch(
    search_tools: dict[str, Callable[..., Awaitable[str]]],
    valid_mzml: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        ingest,
        "DEFAULT_POLICY",
        security.SecurityPolicy(allowed_root=valid_mzml.parent),
    )
    with pytest.raises(MsmcpError, match=r"MS1.*MS2"):
        await search_tools["search_library"](
            experimental_file=str(valid_mzml), database_file="library/test_library.txt"
        )


def test_ms1_request_cannot_bypass_dispatch_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    msp = _library(tmp_path, monkeypatch)
    with pytest.raises(MsmcpError, match=r"MS1.*MS2"):
        _run_scan(
            SearchRequest(
                database_file=str(msp),
                experimental_peaks=PEAKS,
                query_ms_level=1,
            )
        )
