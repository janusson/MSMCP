"""Real spectral-library reader and the security boundary it must cross.

``search_library``'s ``database_file`` was never resolved on disk, so a supplied
path was only a seed for a synthetic in-memory library and nothing validated
it.  These tests pin the repaired contract:

* an MSP/NIST-style library file on disk is **read** and searched, and the
  report says so (no synthetic banner);
* the path is routed through the same :class:`~msmcp.security.SecurityPolicy`
  as acquisitions, so a library outside the allowed root is refused before any
  open;
* the reader is strict about malformed input (no silently dropped peaks).

The primary behavioural tests deliberately avoid importing the reader module so
they fail as *assertions* against the old behaviour rather than as import
errors; the reader unit tests import it directly.
"""

from __future__ import annotations

import gzip
from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest

from msmcp.security import PathEscapeError, SecurityPolicy
from msmcp.tools.search import SearchRequest, _run_scan

SearchTool = Callable[..., Awaitable[str]]

# A small, valid NIST/MSP ground-truth library.  Caffeine's peak list is the
# query used by the behavioural tests, so a working reader ranks it first.
GROUND_TRUTH_MSP = """\
# Ground-truth MSP library written by the MSMCP test suite
Name: Caffeine
Formula: C8H10N4O2
PrecursorMZ: 195.0877
Ion_mode: P
Num Peaks: 5
110.0713 40.0
120.0808 100.0
136.0757 60.0
138.0662 420.0
500.1 55.0

Name: Theobromine
Formula: C7H8N4O2
PrecursorMZ: 181.0720
Ion_mode: P
Num Peaks: 3
110.0713 12.0
163.0601 510.0
181.0720 8.0

Name: Glucose
Formula: C6H12O6
PrecursorMZ: 181.0707
Ion_mode: N
Num Peaks: 3
85.0284 30.0
127.0390 45.0
163.0601 100.0
"""

CAFFEINE_PEAKS: list[tuple[float, float]] = [
    (110.0713, 40.0),
    (120.0808, 100.0),
    (136.0757, 60.0),
    (138.0662, 420.0),
    (500.1, 55.0),
]


def _write_msp(path: Path, text: str = GROUND_TRUTH_MSP) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Behavioural: a real MSP file on disk is actually searched
# ---------------------------------------------------------------------------
class TestRealLibrarySearch:
    def test_real_msp_library_is_read_and_searched(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A real MSP library drives the hits; the report is not synthetic."""
        from msmcp import security

        msp = _write_msp(tmp_path / "ground_truth.msp")
        monkeypatch.setattr(
            security, "DEFAULT_POLICY", SecurityPolicy(allowed_root=tmp_path)
        )

        outcome = _run_scan(
            SearchRequest(
                database_file=str(msp),
                experimental_peaks=tuple(CAFFEINE_PEAKS),
                experimental_file="caffeine_experimental.mzML",
            )
        )
        report = outcome.report

        assert "REAL LIBRARY" in report
        assert "SYNTHETIC LIBRARY" not in report
        assert "Caffeine" in report
        # The report must name the file that was actually opened.
        assert "MSP" in report
        assert str(msp) in report

    def test_real_library_provenance_is_recorded(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Provenance records the library as a real, digested source."""
        from msmcp import security

        msp = _write_msp(tmp_path / "ground_truth.msp")
        monkeypatch.setattr(
            security, "DEFAULT_POLICY", SecurityPolicy(allowed_root=tmp_path)
        )

        outcome = _run_scan(
            SearchRequest(
                database_file=str(msp),
                experimental_peaks=tuple(CAFFEINE_PEAKS),
                experimental_file="caffeine_experimental.mzML",
            )
        )
        record = outcome.provenance.to_dict()
        parameters = record["parameters"]

        assert parameters["library_synthetic"] is False
        assert parameters["library_format"] == "MSP"
        assert parameters["library_spectra"] == 3
        assert str(parameters["library_digest"]).startswith("sha256:")
        library_sources = [s for s in record["sources"] if s["path"] == str(msp)]
        assert len(library_sources) == 1
        assert library_sources[0]["backend"] == "msmcp.library"
        assert library_sources[0]["digest"].startswith("sha256:")

    def test_unknown_library_format_falls_back_to_labelled_synthetic(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A path with no reader is still searchable, but never silently."""
        from msmcp import security

        other = tmp_path / "not_a_library.db"
        other.write_text("not an MSP file", encoding="utf-8")
        monkeypatch.setattr(
            security, "DEFAULT_POLICY", SecurityPolicy(allowed_root=tmp_path)
        )

        outcome = _run_scan(
            SearchRequest(
                database_file=str(other),
                experimental_peaks=tuple(CAFFEINE_PEAKS),
                experimental_file="caffeine_experimental.mzML",
            )
        )
        report = outcome.report

        assert "SYNTHETIC LIBRARY" in report
        assert "not opened" in report
        assert outcome.provenance.to_dict()["parameters"]["library_synthetic"] is True


# ---------------------------------------------------------------------------
# Security: the library path crosses the same boundary as acquisitions
# ---------------------------------------------------------------------------
class TestLibrarySecurityBoundary:
    def test_run_scan_refuses_a_library_outside_the_root(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A library path outside the allowed root is refused before any open."""
        from msmcp import security

        allowed = tmp_path / "allowed"
        outside = tmp_path / "outside"
        allowed.mkdir()
        outside.mkdir()
        evil = _write_msp(outside / "evil.msp")
        monkeypatch.setattr(
            security, "DEFAULT_POLICY", SecurityPolicy(allowed_root=allowed)
        )

        with pytest.raises(PathEscapeError, match="outside the allowed root"):
            _run_scan(
                SearchRequest(
                    database_file=str(evil),
                    experimental_peaks=tuple(CAFFEINE_PEAKS),
                    experimental_file="caffeine_experimental.mzML",
                )
            )

    async def test_search_library_refuses_a_library_outside_the_root(
        self,
        search_tools: dict[str, SearchTool],
        valid_mzml: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The tool refuses an out-of-root library before dispatching a job."""
        from msmcp import ingest, security

        allowed = tmp_path / "allowed"
        outside = tmp_path / "outside"
        allowed.mkdir()
        outside.mkdir()
        query = allowed / "query.mzML"
        query.write_text(valid_mzml.read_text(encoding="utf-8"), encoding="utf-8")
        evil = _write_msp(outside / "evil.msp")

        policy = SecurityPolicy(allowed_root=allowed)
        monkeypatch.setattr(ingest, "DEFAULT_POLICY", policy)
        monkeypatch.setattr(security, "DEFAULT_POLICY", policy)

        search_library = search_tools["search_library"]
        with pytest.raises(PathEscapeError, match="outside the allowed root"):
            await search_library(
                experimental_file=str(query), database_file=str(evil)
            )

    def test_library_is_opened_read_only(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The reader never needs write permission on the library file."""
        from msmcp import security

        msp = _write_msp(tmp_path / "ground_truth.msp")
        msp.chmod(0o444)  # read-only on disk
        monkeypatch.setattr(
            security, "DEFAULT_POLICY", SecurityPolicy(allowed_root=tmp_path)
        )

        outcome = _run_scan(
            SearchRequest(
                database_file=str(msp),
                experimental_peaks=tuple(CAFFEINE_PEAKS),
                experimental_file="caffeine_experimental.mzML",
            )
        )
        assert "REAL LIBRARY" in outcome.report
        assert msp.read_text(encoding="utf-8").startswith("# Ground-truth")


# ---------------------------------------------------------------------------
# Reader unit tests — MSP/NIST text parsing
# ---------------------------------------------------------------------------
class TestMSPReader:
    def test_provider_describes_the_library(self, tmp_path: Path) -> None:
        from msmcp.library import get_library_provider

        msp = _write_msp(tmp_path / "lib.msp")
        policy = SecurityPolicy(allowed_root=tmp_path)
        provider = get_library_provider(str(msp), policy)
        assert provider is not None
        info = provider.describe()

        assert info.format == "MSP"
        assert info.n_spectra == 3
        assert info.digest is not None and info.digest.startswith("sha256:")
        assert Path(info.path) == msp.resolve()

    def test_iter_spectra_returns_float64_peaks_and_metadata(
        self, tmp_path: Path
    ) -> None:
        from msmcp.library import get_library_provider

        msp = _write_msp(tmp_path / "lib.msp")
        provider = get_library_provider(str(msp), SecurityPolicy(allowed_root=tmp_path))
        assert provider is not None
        spectra = [s for chunk in provider.iter_spectra(2) for s in chunk]

        by_name = {s.compound_name: s for s in spectra}
        assert set(by_name) == {"Caffeine", "Theobromine", "Glucose"}

        caffeine = by_name["Caffeine"]
        assert caffeine.precursor_mz == pytest.approx(195.0877)
        assert caffeine.formula == "C8H10N4O2"
        assert len(caffeine.peaks) == 5
        assert caffeine.peaks[0] == pytest.approx((110.0713, 40.0))
        assert all(isinstance(mz, float) for mz, _ in caffeine.peaks)
        assert caffeine.metadata["IONMODE"] == "P"

        # chunking must not lose or duplicate spectra
        assert [s.index for s in spectra] == sorted(s.index for s in spectra)

    def test_num_peaks_mismatch_is_rejected(self, tmp_path: Path) -> None:
        from msmcp.errors import MalformedFileError
        from msmcp.library import get_library_provider

        bad = tmp_path / "bad.msp"
        bad.write_text(
            "Name: Broken\nPrecursorMZ: 200.0\nNum Peaks: 5\n100.0 1.0\n200.0 2.0\n",
            encoding="utf-8",
        )
        provider = get_library_provider(str(bad), SecurityPolicy(allowed_root=tmp_path))
        assert provider is not None
        with pytest.raises(MalformedFileError, match="Num Peaks"):
            list(provider.iter_spectra(10))

    def test_malformed_peak_line_is_rejected(self, tmp_path: Path) -> None:
        from msmcp.errors import MalformedFileError
        from msmcp.library import get_library_provider

        bad = tmp_path / "bad.msp"
        bad.write_text(
            "Name: Broken\n100.0 1.0\nnot_a_number\n", encoding="utf-8"
        )
        provider = get_library_provider(str(bad), SecurityPolicy(allowed_root=tmp_path))
        assert provider is not None
        with pytest.raises(MalformedFileError, match="peak line"):
            list(provider.iter_spectra(10))

    def test_missing_file_is_inaccessible(self, tmp_path: Path) -> None:
        from msmcp.errors import InaccessiblePathError
        from msmcp.library import get_library_provider

        with pytest.raises(InaccessiblePathError, match="not found"):
            get_library_provider(
                str(tmp_path / "absent.msp"), SecurityPolicy(allowed_root=tmp_path)
            )

    def test_gzipped_msp_is_read(self, tmp_path: Path) -> None:
        from msmcp.library import get_library_provider

        path = tmp_path / "lib.msp.gz"
        path.write_bytes(gzip.compress(GROUND_TRUTH_MSP.encode("utf-8")))
        provider = get_library_provider(str(path), SecurityPolicy(allowed_root=tmp_path))
        assert provider is not None
        assert provider.describe().n_spectra == 3

    def test_semicolon_separated_peaks_are_split(self, tmp_path: Path) -> None:
        from msmcp.library import get_library_provider

        path = tmp_path / "oneline.msp"
        path.write_text(
            "Name: OneLine\nPrecursorMZ: 200.0\nNum Peaks: 3\n"
            "100.0 1.0; 150.0 2.0; 200.0 3.0\n",
            encoding="utf-8",
        )
        provider = get_library_provider(str(path), SecurityPolicy(allowed_root=tmp_path))
        assert provider is not None
        spectrum = next(s for chunk in provider.iter_spectra(10) for s in chunk)
        assert spectrum.peaks == ((100.0, 1.0), (150.0, 2.0), (200.0, 3.0))
