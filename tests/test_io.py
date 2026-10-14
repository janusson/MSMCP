"""Behavioural tests for the I/O tools.

These tools are the front door to a run: they read a real file through the
security boundary and either summarise it (``load_mzml_summary``), register a
spectrum server-side (``load_spectrum``), or — with ``list_spectra`` — index the
spectra so a caller can choose which one is worth loading.

``list_spectra`` is the discovery step, so it is tested the way a host would use
it: find the MS2 candidates, then hand one index to ``load_spectrum``.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from pydantic import ValidationError

from msmcp import ingest
from msmcp.security import PathEscapeError, SecurityPolicy
from msmcp.tools import io as msmcp_io
from msmcp.tools.io import ListSpectraInput

IOTools = dict[str, Callable[..., str]]


@pytest.fixture()
def data_root(valid_mzml: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Confine the I/O tools to this test's temporary directory.

    The tool modules (and the ingestion dispatcher) each bind ``DEFAULT_POLICY``
    in their own namespace, so both must be redirected — mirroring how the
    server is configured once, before it starts serving.
    """
    policy = SecurityPolicy(allowed_root=valid_mzml.parent)
    monkeypatch.setattr(msmcp_io, "DEFAULT_POLICY", policy)
    monkeypatch.setattr(ingest, "DEFAULT_POLICY", policy)
    yield valid_mzml.parent


class TestListSpectra:
    def test_lists_every_spectrum_in_index_order(
        self, io_tools: IOTools, valid_mzml: Path, data_root: Path
    ) -> None:
        table = io_tools["list_spectra"](str(valid_mzml))
        assert table.startswith("## Spectrum Index")
        assert "3 of 3 spectra" in table
        assert "| Index | MS | RT (min) | Precursor m/z | Peaks |" in table
        assert (
            table.index("| 0 | MS") < table.index("| 1 | MS") < table.index("| 2 | MS")
        )
        # The precursor-bearing spectrum reports its precursor m/z.
        assert "300.0000" in table

    def test_ms_level_filter_isolates_ms2_spectra(
        self, io_tools: IOTools, mzml_with_empty_scan: Path, data_root: Path
    ) -> None:
        table = io_tools["list_spectra"](str(mzml_with_empty_scan), ms_level=2)
        assert "**Filter:** MS level == 2" in table
        assert "| 0 | MS" not in table  # the MS1 survey scan is excluded
        assert "| 1 | MS" in table
        assert "| 2 | MS" in table
        # A legitimately empty MS2 is still listed, with its precursor intact.
        assert "89.5077" in table
        assert "2 of 3 spectra" in table

    def test_min_peaks_drops_empty_spectra(
        self, io_tools: IOTools, mzml_with_empty_scan: Path, data_root: Path
    ) -> None:
        table = io_tools["list_spectra"](str(mzml_with_empty_scan), min_peaks=1)
        assert "at least 1 peaks" in table
        assert "| 1 | MS" not in table  # the 0-peak MS2 is filtered out
        assert "| 0 | MS" in table
        assert "| 2 | MS" in table

    def test_max_spectra_reports_that_the_listing_was_truncated(
        self, io_tools: IOTools, valid_mzml: Path, data_root: Path
    ) -> None:
        table = io_tools["list_spectra"](str(valid_mzml), max_spectra=1)
        assert "stopped at the" in table
        assert "1 spectra" in table
        assert "| 0 | MS" in table
        assert "| 1 | MS" not in table

    def test_a_filter_that_matches_nothing_says_so(
        self, io_tools: IOTools, valid_mzml: Path, data_root: Path
    ) -> None:
        table = io_tools["list_spectra"](str(valid_mzml), ms_level=3)
        assert "_No spectra matched the filter._" in table
        assert "of 3 spectra" in table

    def test_the_scan_stops_at_the_request_spectrum_budget(
        self, io_tools: IOTools, valid_mzml: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A low ``MSMCP_MAX_SPECTRA`` bounds how much of the file is read.

        The caller's row cap is validated against the budget (raising when it is
        exceeded); this covers the case where the request is within budget but
        matches are sparse, so the scan must still stop at the budget instead of
        reading the whole file.
        """
        policy = SecurityPolicy(allowed_root=valid_mzml.parent, max_spectra=2)
        monkeypatch.setattr(msmcp_io, "DEFAULT_POLICY", policy)
        monkeypatch.setattr(ingest, "DEFAULT_POLICY", policy)

        table = io_tools["list_spectra"](str(valid_mzml), ms_level=3, max_spectra=2)
        assert "`MSMCP_MAX_SPECTRA` scan budget of 2" in table
        assert "of the first 2 spectra" in table
        assert "_No spectra matched the filter._" in table

    def test_the_scan_stays_inside_the_allowed_root(
        self,
        io_tools: IOTools,
        valid_mzml: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        nested = valid_mzml.parent / "nested"
        nested.mkdir()
        policy = SecurityPolicy(allowed_root=nested)
        monkeypatch.setattr(msmcp_io, "DEFAULT_POLICY", policy)
        monkeypatch.setattr(ingest, "DEFAULT_POLICY", policy)

        with pytest.raises(PathEscapeError, match="outside the allowed root"):
            io_tools["list_spectra"](str(valid_mzml))


class TestListSpectraInput:
    def test_rejects_a_row_cap_above_the_advertised_maximum(self) -> None:
        with pytest.raises(ValidationError):
            ListSpectraInput(file_path="run.mzML", max_spectra=1001)

    def test_rejects_a_zero_ms_level(self) -> None:
        with pytest.raises(ValidationError):
            ListSpectraInput(file_path="run.mzML", ms_level=0)

    def test_accepts_the_documented_defaults(self) -> None:
        validated = ListSpectraInput(file_path="run.mzML")
        assert validated.ms_level is None
        assert validated.max_spectra == 200
        assert validated.min_peaks == 0
