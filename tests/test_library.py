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
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:  # the reader module stays un-imported at runtime on purpose
    from msmcp.library import LibraryProvider

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

    def test_an_empty_library_is_searched_and_reports_no_hits(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An empty library is legal: it is read for real, finds nothing, says so.

        Zero spectra is a *data* condition, not a parse failure — the report must
        describe the real (empty) library rather than fall back to claiming the
        query hit anything.
        """
        from msmcp import security

        empty = tmp_path / "empty.msp"
        empty.write_text("# a library with no records\n", encoding="utf-8")
        monkeypatch.setattr(
            security, "DEFAULT_POLICY", SecurityPolicy(allowed_root=tmp_path)
        )

        outcome = _run_scan(
            SearchRequest(
                database_file=str(empty),
                experimental_peaks=tuple(CAFFEINE_PEAKS),
                experimental_file="caffeine_experimental.mzML",
            )
        )
        report = outcome.report

        assert "REAL LIBRARY" in report
        assert "SYNTHETIC LIBRARY" not in report
        assert "No hits passed the significance threshold" in report
        parameters = outcome.provenance.to_dict()["parameters"]
        assert parameters["library_synthetic"] is False
        assert parameters["library_spectra"] == 0


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
            await search_library(experimental_file=str(query), database_file=str(evil))

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
# Pre-dispatch validation: a malformed library fails like a malformed query
# ---------------------------------------------------------------------------
class TestLibraryValidatedBeforeDispatch:
    """The library probe matches the query path, which is read before dispatch."""

    async def test_search_library_refuses_a_malformed_library_before_dispatch(
        self,
        search_tools: dict[str, SearchTool],
        valid_mzml: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A file that is not the format its suffix claims is an immediate error.

        The library is probed before a job is submitted, so a host gets a typed
        ``MalformedFileError`` from the tool call instead of a job ID that later
        fails in the background.
        """
        from msmcp import ingest, security
        from msmcp.errors import MalformedFileError

        root = tmp_path / "root"
        root.mkdir()
        query = root / "query.mzML"
        query.write_text(valid_mzml.read_text(encoding="utf-8"), encoding="utf-8")
        # A .msp that claims five peaks and supplies one.
        bad = root / "not_really.msp"
        bad.write_text("Name: Broken\nNum Peaks: 5\n100.0 1.0\n", encoding="utf-8")

        policy = SecurityPolicy(allowed_root=root)
        monkeypatch.setattr(ingest, "DEFAULT_POLICY", policy)
        monkeypatch.setattr(security, "DEFAULT_POLICY", policy)

        with pytest.raises(MalformedFileError, match="Num Peaks"):
            await search_tools["search_library"](
                experimental_file=str(query), database_file=str(bad)
            )

    async def test_a_malformed_query_and_a_malformed_library_fail_the_same_way(
        self,
        search_tools: dict[str, SearchTool],
        valid_mzml: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Both halves of the pipeline are refused pre-dispatch, not in a job."""
        from msmcp import ingest, security
        from msmcp.errors import MalformedFileError, MsmcpError

        root = tmp_path / "root"
        root.mkdir()
        query = root / "query.mzML"
        query.write_text(valid_mzml.read_text(encoding="utf-8"), encoding="utf-8")
        good = _write_msp(root / "good.msp")
        broken = root / "broken.msp"
        broken.write_text("Name: Broken\n100.0 1.0\nnonsense\n", encoding="utf-8")
        torn = root / "torn.mzML"
        torn.write_text("<mzML><spectrum", encoding="utf-8")

        policy = SecurityPolicy(allowed_root=root)
        monkeypatch.setattr(ingest, "DEFAULT_POLICY", policy)
        monkeypatch.setattr(security, "DEFAULT_POLICY", policy)

        search_library = search_tools["search_library"]

        # The query is read first, so a torn one is refused before the library
        # is even touched.
        with pytest.raises(MsmcpError):
            await search_library(experimental_file=str(torn), database_file=str(good))

        # A good query with a broken library is refused by the library probe.
        with pytest.raises(MalformedFileError, match="peak line"):
            await search_library(
                experimental_file=str(query), database_file=str(broken)
            )


# ---------------------------------------------------------------------------
# Reader unit tests — MGF parsing
# ---------------------------------------------------------------------------
def _write_mgf(path: Path) -> Path:
    """Write a minimal MGF library for tests."""
    content = """\
BEGIN IONS
TITLE=Spectrum 1
PEPMASS=195.088
RTINSECONDS=120.0
MSLEVEL=2
110.07 100.0
120.08 50.0
END IONS

BEGIN IONS
TITLE=Spectrum 2
PEPMASS=200.0
150.0 75.0
END IONS
"""
    path.write_text(content, encoding="utf-8")
    return path


class TestMGFReader:
    def test_provider_describes_the_library(self, tmp_path: Path) -> None:
        from msmcp.library import get_library_provider

        mgf = _write_mgf(tmp_path / "lib.mgf")
        policy = SecurityPolicy(allowed_root=tmp_path)
        provider = get_library_provider(str(mgf), policy)
        assert provider is not None
        info = provider.describe()

        assert info.format == "MGF"
        assert info.n_spectra == 2
        assert info.digest is not None and info.digest.startswith("sha256:")
        assert Path(info.path) == mgf.resolve()

    def test_iter_spectra_returns_float64_peaks_and_metadata(
        self, tmp_path: Path
    ) -> None:
        from msmcp.library import get_library_provider

        mgf = _write_mgf(tmp_path / "lib.mgf")
        provider = get_library_provider(str(mgf), SecurityPolicy(allowed_root=tmp_path))

        chunks = list(provider.iter_spectra(chunk_size=1))
        assert len(chunks) == 2
        assert [len(c) for c in chunks] == [1, 1]

        s1 = chunks[0][0]
        assert s1.compound_name == "Spectrum 0"
        assert s1.precursor_mz == 195.088
        assert len(s1.peaks) == 2
        assert s1.peaks[0] == (110.07, 100.0)
        assert s1.peaks[1] == (120.08, 50.0)
        assert isinstance(s1.peaks[0][0], float)
        assert s1.metadata["MSLEVEL"] == "2"
        assert s1.metadata["RTINMINUTES"] == "2.0"

        s2 = chunks[1][0]
        assert s2.compound_name == "Spectrum 1"
        assert s2.precursor_mz == 200.0
        assert len(s2.peaks) == 1
        assert s2.peaks[0] == (150.0, 75.0)


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
        bad.write_text("Name: Broken\n100.0 1.0\nnot_a_number\n", encoding="utf-8")
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
        provider = get_library_provider(
            str(path), SecurityPolicy(allowed_root=tmp_path)
        )
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
        provider = get_library_provider(
            str(path), SecurityPolicy(allowed_root=tmp_path)
        )
        assert provider is not None
        spectrum = next(s for chunk in provider.iter_spectra(10) for s in chunk)
        assert spectrum.peaks == ((100.0, 1.0), (150.0, 2.0), (200.0, 3.0))

    def test_colon_separated_peaks_are_split(self, tmp_path: Path) -> None:
        """The ``m/z:intensity`` spelling is read, not refused.

        Real files in the Fiehn/Golm collection (``GMD_20111121_*_MSP.msp``)
        write several ``mz:intensity`` pairs per line.  Before this they were
        rejected outright as a non-numeric peak line, so five on-disk libraries
        were unusable.
        """
        from msmcp.library import get_library_provider

        path = tmp_path / "colon.msp"
        path.write_text(
            "Name: ColonStyle\nPrecursorMZ: 200.0\nNum Peaks: 4\n"
            "70:10 76:35 77:1000 78:110\n",
            encoding="utf-8",
        )
        provider = get_library_provider(
            str(path), SecurityPolicy(allowed_root=tmp_path)
        )
        assert provider is not None
        spectrum = next(s for chunk in provider.iter_spectra(10) for s in chunk)
        assert spectrum.peaks == (
            (70.0, 10.0),
            (76.0, 35.0),
            (77.0, 1000.0),
            (78.0, 110.0),
        )

    def test_colon_and_semicolon_spellings_agree(self, tmp_path: Path) -> None:
        """The same peaks give the same result whichever spelling is used.

        Both of these carry several peaks on one line; the plain whitespace form
        does not (it is one pair per line), so it is not the comparison to make.
        """
        from msmcp.library import get_library_provider

        pairs = [(70.0, 10.0), (76.0, 35.0), (77.0, 1000.0)]
        bodies = {
            "colon": " ".join(f"{mz:g}:{i:g}" for mz, i in pairs),
            "semicolon": "; ".join(f"{mz:g} {i:g}" for mz, i in pairs),
        }
        results = {}
        for name, body in bodies.items():
            path = tmp_path / f"{name}.msp"
            path.write_text(
                f"Name: {name}\nNum Peaks: {len(pairs)}\n{body}\n", encoding="utf-8"
            )
            provider = get_library_provider(
                str(path), SecurityPolicy(allowed_root=tmp_path)
            )
            assert provider is not None
            spectrum = next(s for chunk in provider.iter_spectra(10) for s in chunk)
            results[name] = spectrum.peaks
        assert results["colon"] == results["semicolon"] == tuple(pairs)

    @pytest.mark.parametrize(
        "line",
        [
            "70:10 76 35",  # colon field first, bare pair after
            "70 10 76:35",  # bare pair first, colon field after
            "70 10 76:35 78:110",  # a whole colon pair hidden after a bare one
        ],
    )
    def test_a_line_mixing_the_two_spellings_is_refused(
        self, tmp_path: Path, line: str
    ) -> None:
        """An ambiguous line raises rather than being silently reinterpreted.

        The ordering matters.  Recognising the colon form only when *some* field
        carries a colon lets ``70 10 76:35`` fall through to the whitespace path,
        which would keep ``(70, 10)`` and drop the rest without saying so.
        """
        from msmcp.errors import MalformedFileError
        from msmcp.library import get_library_provider

        path = tmp_path / "mixed.msp"
        path.write_text(f"Name: Mixed\nNum Peaks: 1\n{line}\n", encoding="utf-8")
        provider = get_library_provider(
            str(path), SecurityPolicy(allowed_root=tmp_path)
        )
        assert provider is not None
        with pytest.raises(MalformedFileError, match="mixes"):
            list(provider.iter_spectra(10))

    def test_a_whitespace_line_with_more_than_one_pair_is_refused(
        self, tmp_path: Path
    ) -> None:
        """The whitespace form is one pair per line, so extra fields are an error.

        Reading the first two fields and discarding the rest would drop peaks
        silently — the failure mode this reader exists to avoid — and the
        ``Num Peaks`` check only catches it by coincidence (here it would not:
        one pair declared, one pair kept).
        """
        from msmcp.errors import MalformedFileError
        from msmcp.library import get_library_provider

        path = tmp_path / "overflow.msp"
        path.write_text("Name: Overflow\nNum Peaks: 1\n70 10 76 35\n", encoding="utf-8")
        provider = get_library_provider(
            str(path), SecurityPolicy(allowed_root=tmp_path)
        )
        assert provider is not None
        with pytest.raises(MalformedFileError, match="is not"):
            list(provider.iter_spectra(10))

    def test_colon_form_with_a_missing_intensity_is_refused(
        self, tmp_path: Path
    ) -> None:
        """A malformed field in the colon spelling is still an error."""
        from msmcp.errors import MalformedFileError
        from msmcp.library import get_library_provider

        path = tmp_path / "badcolon.msp"
        path.write_text("Name: BadColon\nNum Peaks: 2\n70:10 76:\n", encoding="utf-8")
        provider = get_library_provider(
            str(path), SecurityPolicy(allowed_root=tmp_path)
        )
        assert provider is not None
        with pytest.raises(MalformedFileError, match="non-numeric"):
            list(provider.iter_spectra(10))


# ---------------------------------------------------------------------------
# Metadata-only records: a deposition without a spectrum is data, not corruption
# ---------------------------------------------------------------------------
# Verbatim from GNPS-LIBRARY.msp: fourteen header lines, no "Num Peaks" header,
# no peak lines.  GNPS-NIST14-MATCHES.msp (Nalidixic acid) and
# DEREPLICATOR_IDENTIFIED_LIBRARY.msp (Puwainaphycin C) each carry an identical
# shape.  One such record used to make the entire library unreadable, so a
# 139 MB library could not be searched at all.
METADATA_ONLY_MSP = """\
Name: Caffeine
Formula: C8H10N4O2
PrecursorMZ: 195.0877
Num Peaks: 5
110.0713 40.0
120.0808 100.0
136.0757 60.0
138.0662 420.0
500.1 55.0

NAME: Ferrichrome
PRECURSORMZ: 763.0
PRECURSORTYPE: M+Na
FORMULA: C27H42FeN9NaO12+
Ontology:
INCHIKEY: QNVPQTXXHKIFLL-UHFFFAOYSA-N
INCHI:
SMILES: CC(=O)N(CCCC1C(=O)NC(C(=O)NC(C(=O)NCC(=O)NCC(=O)NCC(=O)N1)CCCN(C(=O)C)[O-])CCCN(C(=O)C)[O-])[O-].[Fe+3][Na+]
RETENTIONTIME: CCS:
IONMODE: Positive
INSTRUMENTTYPE: DI-ESI-Hybrid FT
INSTRUMENT: Hybrid FT
COLLISIONENERGY:
Comment: DB#=CCMSLIB00000078897; origin=GNPS

Name: Theobromine
Formula: C7H8N4O2
PrecursorMZ: 181.0720
Num Peaks: 3
110.0713 12.0
163.0601 510.0
181.0720 8.0
"""


class TestMetadataOnlyRecords:
    """A record with no peaks is skipped, counted, and hides nothing."""

    def _provider(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        text: str = METADATA_ONLY_MSP,
    ) -> tuple[LibraryProvider, Path]:
        from msmcp import security
        from msmcp.library import get_library_provider

        path = tmp_path / "with_metadata_only.msp"
        path.write_text(text, encoding="utf-8")
        monkeypatch.setattr(
            security, "DEFAULT_POLICY", SecurityPolicy(allowed_root=tmp_path)
        )
        provider = get_library_provider(
            str(path), SecurityPolicy(allowed_root=tmp_path)
        )
        return provider, path

    def test_the_records_around_it_are_still_read(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A peak-less record must not truncate the records that follow it."""
        provider, _ = self._provider(tmp_path, monkeypatch)
        names = [
            spectrum.compound_name
            for chunk in provider.iter_spectra(10)
            for spectrum in chunk
        ]
        assert names == ["Caffeine", "Theobromine"]

    def test_the_skipped_record_is_counted_not_hidden(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Dropping a record silently would be its own kind of lie."""
        provider, _ = self._provider(tmp_path, monkeypatch)
        info = provider.describe()

        assert info.n_spectra == 2
        assert info.n_records_without_peaks == 1

    def test_a_declared_zero_peak_record_is_also_skipped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``Num Peaks: 0`` agrees with the peak lines: still nothing to match."""
        text = (
            "Name: Declared-Empty\nPrecursorMZ: 300.0\nNum Peaks: 0\n\n"
            "Name: Real\nPrecursorMZ: 301.0\nNum Peaks: 2\n200.0 1.0\n201.0 2.0\n"
        )
        provider, _ = self._provider(tmp_path, monkeypatch, text)
        names = [
            spectrum.compound_name
            for chunk in provider.iter_spectra(10)
            for spectrum in chunk
        ]
        assert names == ["Real"]
        assert provider.describe().n_records_without_peaks == 1

    def test_a_num_peaks_mismatch_is_still_malformed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Corruption is still corruption; the strictness is not weakened."""
        from msmcp.errors import MalformedFileError

        text = "Name: Broken\nPrecursorMZ: 300.0\nNum Peaks: 5\n200.0 1.0\n201.0 2.0\n"
        provider, _ = self._provider(tmp_path, monkeypatch, text)
        with pytest.raises(MalformedFileError, match="declares"):
            list(provider.iter_spectra(10))

    def test_peaks_without_a_num_peaks_header_are_still_read(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Only *empty* records are skipped; the header is not required."""
        text = "Name: NoHeader\nPrecursorMZ: 300.0\n200.0 1.0\n201.0 2.0\n"
        provider, _ = self._provider(tmp_path, monkeypatch, text)
        spectra = [s for chunk in provider.iter_spectra(10) for s in chunk]
        assert len(spectra) == 1
        assert spectra[0].peaks == ((200.0, 1.0), (201.0, 2.0))
        assert provider.describe().n_records_without_peaks == 0
