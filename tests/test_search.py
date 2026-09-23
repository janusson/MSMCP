"""Tests for the executor-backed spectral library search tools.

Covers the dispatcher/poller contract (``search_library`` submits to the
:class:`~msmcp.execution.executor.JobExecutor`; ``check_search_status`` polls
it), the scorer routing (classical vs. foundation-model embeddings), the
truthfulness of the synthetic-library banner, and the negative paths
(malformed / missing / unreferenced queries).

The query spectrum is always real data now, so the round-trip tests feed the
tools a genuine mzML fixture rather than a path string.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import ClassVar

import numpy as np
import pytest
from pydantic import ValidationError

from msmcp.models.embeddings import DreaMSEmbedder, LSMMS2Embedder
from msmcp.state import store as reference_store
from msmcp.state.pointers import UnknownReferenceError
from msmcp.tools import search
from msmcp.tools.search import (
    SearchRequest,
    _build_mock_database,
    _build_scorer,
    _cosine,
    _decoy_spectrum,
    _estimate_empirical_p,
    _permutable,
    _run_scan,
    _scoring_label,
)
from msmcp.tools.similarity import _cosine as _vector_cosine

DB_FILE = "library/test_library.db"

QUERY_PEAKS: list[tuple[float, float]] = [
    (110.0713, 40.0),
    (120.0808, 100.0),
    (136.0757, 60.0),
    (500.1, 55.0),
]


def _scan(
    database_file: str = DB_FILE,
    scoring_method: str = "classical",
    peaks: list[tuple[float, float]] | None = None,
    *,
    experimental_file: str | None = "experimental/test_spectrum.mzML",
) -> str:
    """Run the CPU-bound scan directly with real query peaks."""
    return _run_scan(
        SearchRequest(
            database_file=database_file,
            experimental_peaks=tuple(peaks if peaks is not None else QUERY_PEAKS),
            scoring_method=scoring_method,  # type: ignore[arg-type]
            experimental_file=experimental_file,
        )
    ).report


# ---------------------------------------------------------------------------
# Search components — synthetic library, report builder
# ---------------------------------------------------------------------------
class TestSearchComponents:
    def test_mock_database_schema_and_rows(self) -> None:
        conn = _build_mock_database(n_spectra=25, seed=7)
        try:
            n_spec = conn.execute("SELECT COUNT(*) FROM spectra").fetchone()[0]
            n_peaks = conn.execute("SELECT COUNT(*) FROM peaks").fetchone()[0]
            assert n_spec == 25
            assert n_peaks >= 25  # every spectrum carries at least one peak
        finally:
            conn.close()

    def test_scan_report_well_formed(self) -> None:
        """The report builder renders a complete Markdown report."""
        report = _scan()
        assert "## Spectral Library Search Results" in report
        assert "Scoring method: classical (greedy peak matching, ±0.02 Da)" in report
        # The synthetic library is hash-seeded per process, so the number of
        # threshold-passing hits is not deterministic — assert the report is
        # well-formed in either outcome instead.
        assert ("| Rank | Compound" in report) or (
            "No hits passed the significance threshold" in report
        )

    def test_scan_requires_a_non_empty_query(self) -> None:
        """An empty query is a programming error, not a zero-score search."""
        with pytest.raises(search.MsmcpError, match="no peaks"):
            _scan(peaks=[])


# ---------------------------------------------------------------------------
# Null model — what the p-values are actually calibrated against
# ---------------------------------------------------------------------------
class TestNullModelIntegrity:
    """The null distribution has to be *different* from the target one.

    If a decoy scores exactly what its target scores, the p-value column is
    computed against a copy of the target distribution and means nothing.  That
    is what happened when decoys were built by shuffling the (m/z, intensity)
    pair list: a no-op for a scorer that sorts or hashes peaks.
    """

    PEAKS: ClassVar[list[tuple[float, float]]] = [
        (100.0, 10.0),
        (150.0, 40.0),
        (200.0, 5.0),
        (250.0, 90.0),
        (300.0, 25.0),
    ]

    def test_decoy_keeps_fragments_but_not_the_pairing(self) -> None:
        rng = random.Random(11)
        decoy = _decoy_spectrum(self.PEAKS, rng)
        assert [mz for mz, _ in decoy] == [mz for mz, _ in self.PEAKS]
        assert sorted(i for _, i in decoy) == sorted(i for _, i in self.PEAKS)
        assert decoy != self.PEAKS

    def test_decoy_is_never_the_target(self) -> None:
        rng = random.Random(3)
        for _ in range(100):
            assert _decoy_spectrum(self.PEAKS, rng) != self.PEAKS

    def test_decoy_of_a_single_peak_spectrum_is_refused(self) -> None:
        with pytest.raises(ValueError, match="at least two peaks"):
            _decoy_spectrum([(100.0, 5.0)], random.Random(0))

    def test_permutability_gate(self) -> None:
        assert _permutable(self.PEAKS)
        # uniform intensities carry no pattern for a decoy to break
        assert not _permutable([(100.0, 5.0), (200.0, 5.0)])
        assert not _permutable([(100.0, 5.0)])

    def test_empirical_p_value_counts_ties_as_null_matches(self) -> None:
        """Ties must be counted: real null distributions are mostly exact zeros."""
        # 5 nulls, all >= 0.0 → p = (1 + 5) / (1 + 5) = 1.0
        assert _estimate_empirical_p([0.0], [0.0, 0.0, 0.0, 0.0, 0.9]) == [1.0]
        # nothing at or above 1.0 → p = (1 + 0) / (1 + 5)
        assert _estimate_empirical_p([1.0], [0.0, 0.0, 0.0, 0.0, 0.9]) == pytest.approx(
            [1.0 / 6.0]
        )


# ---------------------------------------------------------------------------
# Scorer routing — classical vs. foundation-model embeddings
# ---------------------------------------------------------------------------
class TestScorerRouting:
    PEPTIDE: ClassVar[list[tuple[float, float]]] = [
        (110.0713, 40.0),
        (120.0808, 100.0),
        (136.0757, 60.0),
    ]
    RELATED: ClassVar[list[tuple[float, float]]] = [
        (110.0713, 40.0),
        (120.0808, 100.0),
        (500.1, 55.0),
    ]

    def test_classical_scorer_matches_peak_cosine(self) -> None:
        scorer = _build_scorer("classical")
        assert scorer.score(self.PEPTIDE, self.RELATED) == pytest.approx(
            _cosine(self.PEPTIDE, self.RELATED)
        )

    def test_classical_score_many_agrees_with_score(self) -> None:
        """The batch path the null model uses must match the single-pair path."""
        scorer = _build_scorer("classical")
        references = [self.RELATED, self.PEPTIDE, [], [(500.0, 1.0)]]
        batched = scorer.score_many(self.PEPTIDE, references)
        assert batched == pytest.approx(
            [scorer.score(self.PEPTIDE, reference) for reference in references]
        )

    def test_classical_scorer_rejects_a_non_positive_tolerance(self) -> None:
        from msmcp.models.scoring import ClassicalScorer

        with pytest.raises(ValueError, match="tolerance must be positive"):
            ClassicalScorer(0.0)

    @pytest.mark.parametrize(
        ("method", "embedder_cls"),
        [("dreams", DreaMSEmbedder), ("lsm-ms2", LSMMS2Embedder)],
    )
    def test_embedding_scorer_matches_embedding_cosine(
        self, method: str, embedder_cls: type[DreaMSEmbedder | LSMMS2Embedder]
    ) -> None:
        scorer = _build_scorer(method)
        embedder = embedder_cls()
        expected = _vector_cosine(
            embedder.embed_spectrum(np.asarray(self.PEPTIDE, dtype=np.float64)),
            embedder.embed_spectrum(np.asarray(self.RELATED, dtype=np.float64)),
        )
        assert scorer.score(self.PEPTIDE, self.RELATED) == pytest.approx(expected)

    def test_embedding_score_many_agrees_with_score(self) -> None:
        """The batch path embeds the query once; the scores must not change."""
        scorer = _build_scorer("dreams")
        references = [self.RELATED, self.PEPTIDE, [], [(500.0, 1.0)]]
        batched = scorer.score_many(self.PEPTIDE, references)
        assert batched == pytest.approx(
            [scorer.score(self.PEPTIDE, reference) for reference in references]
        )

    def test_embedding_scorer_handles_empty_peaks(self) -> None:
        scorer = _build_scorer("dreams")
        assert scorer.score([], self.PEPTIDE) == 0.0
        assert scorer.score(self.PEPTIDE, []) == 0.0
        assert scorer.score_many([], [self.PEPTIDE]) == [0.0]

    def test_unknown_method_raises(self) -> None:
        with pytest.raises(ValueError, match="dreams, lsm-ms2"):
            _build_scorer("specter2")

    def test_scoring_label(self) -> None:
        assert _scoring_label("classical") == (
            "classical (greedy peak matching, ±0.02 Da)"
        )
        assert _scoring_label("dreams") == (
            "DreaMS deep embedding (1024-d, mock (dev/test-only, not a learned model))"
        )
        assert _scoring_label("lsm-ms2") == (
            "LSM-MS2 deep embedding (1024-d, mock (dev/test-only, not a learned model))"
        )


# ---------------------------------------------------------------------------
# Dispatcher → poller round trip against the executor
# ---------------------------------------------------------------------------
@pytest.fixture()
def query_mzml(valid_mzml: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A real mzML fixture the search tools are allowed to read."""
    from msmcp import ingest
    from msmcp.security import SecurityPolicy

    monkeypatch.setattr(
        ingest, "DEFAULT_POLICY", SecurityPolicy(allowed_root=valid_mzml.parent)
    )
    return valid_mzml


class TestSearchDispatcherAndPoller:
    async def _poll_until_final(
        self,
        check: Callable[..., Awaitable[str]],
        job_id: str,
        timeout: float = 180.0,
    ) -> str:
        """Poll check_search_status until the report or a failure is returned."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            out = await check(job_id=job_id)
            if "## Spectral Library Search Results" in out or out.startswith(
                "❌ **Failed**"
            ):
                return out
            assert ("Pending" in out) or ("Running" in out), out
            await asyncio.sleep(0.25)
        pytest.fail(f"search job {job_id} did not finish within {timeout}s")

    async def test_full_round_trip(
        self,
        search_tools: dict[str, Callable[..., Awaitable[str]]],
        query_mzml: Path,
    ) -> None:
        dispatched = await search_tools["search_library"](
            experimental_file=str(query_mzml),
            database_file=DB_FILE,
        )
        assert dispatched.startswith("## Search Dispatched")
        job_id = dispatched.split("`")[1]  # first code span is the job ID
        assert len(job_id) == 32  # uuid4 hex, no dashes

        report = await self._poll_until_final(
            search_tools["check_search_status"], job_id
        )
        assert "## Spectral Library Search Results" in report
        assert "Scoring method: classical (greedy peak matching, ±0.02 Da)" in report
        assert ("| Rank | Compound" in report) or (
            "No hits passed the significance threshold" in report
        )

    async def test_real_query_is_read_and_reported(
        self,
        search_tools: dict[str, Callable[..., Awaitable[str]]],
        query_mzml: Path,
    ) -> None:
        """The query spectrum is genuinely read, and the report says so."""
        dispatched = await search_tools["search_library"](
            experimental_file=str(query_mzml),
            database_file=DB_FILE,
        )
        job_id = dispatched.split("`")[1]
        report = await self._poll_until_final(
            search_tools["check_search_status"], job_id
        )
        assert f"Query spectrum: `{query_mzml}`" in report
        assert "real peaks, read from disk" in report
        assert "SYNTHETIC LIBRARY" in report
        assert "Provenance: search_library" in report

    async def test_spectrum_reference_round_trip(
        self,
        search_tools: dict[str, Callable[..., Awaitable[str]]],
        query_mzml: Path,
    ) -> None:
        """A registered spectrum can drive a search without re-uploading peaks."""
        from msmcp import ingest

        source = ingest.resolve_source(query_mzml)
        spectrum = next(iter(source.iter_spectra()))
        reference = reference_store.store_spectrum(spectrum)
        try:
            dispatched = await search_tools["search_library"](
                spectrum_reference=reference.identifier,
                database_file=DB_FILE,
            )
            assert dispatched.startswith("## Search Dispatched")
            job_id = dispatched.split("`")[1]
            report = await self._poll_until_final(
                search_tools["check_search_status"], job_id
            )
            assert f"reference `{reference.identifier}`" in report
            assert "held server-side" in report
        finally:
            reference_store.release(reference.identifier)

    async def test_dreams_embedding_round_trip(
        self,
        search_tools: dict[str, Callable[..., Awaitable[str]]],
        query_mzml: Path,
    ) -> None:
        """An embedding-scored search states the model in its report."""
        dispatched = await search_tools["search_library"](
            experimental_file=str(query_mzml),
            database_file=DB_FILE,
            scoring_method="dreams",
        )
        job_id = dispatched.split("`")[1]
        report = await self._poll_until_final(
            search_tools["check_search_status"], job_id
        )
        assert (
            "Scoring method: DreaMS deep embedding (1024-d, "
            "mock (dev/test-only, not a learned model))" in report
        )

    async def test_unknown_job_id(
        self, search_tools: dict[str, Callable[..., Awaitable[str]]]
    ) -> None:
        """A well-formed ID with no record is a terminal failure.

        The poller must treat an absent job as failed (e.g. after a server
        restart or post-TTL cleanup) rather than as an ambiguous "unknown"
        state it could loop on forever; only malformed IDs are "unknown".
        """
        import uuid

        job_id = uuid.uuid4().hex
        out = await search_tools["check_search_status"](job_id=job_id)
        assert out.startswith("❌ **Failed (not found)**")
        assert f"no search job `{job_id}` exists" in out

    async def test_malformed_job_id(
        self, search_tools: dict[str, Callable[..., Awaitable[str]]]
    ) -> None:
        out = await search_tools["check_search_status"](job_id="not-a-uuid")
        assert out.startswith("❓ **Unknown Job**")
        assert "not a valid job ID" in out

    @pytest.mark.parametrize("bad", ["specter2", "cosine", "DreaMS", ""])
    async def test_invalid_scoring_method_raises_validation_error(
        self,
        search_tools: dict[str, Callable[..., Awaitable[str]]],
        bad: str,
    ) -> None:
        with pytest.raises(ValidationError):
            await search_tools["search_library"](
                experimental_file="a.mzML",
                database_file=DB_FILE,
                scoring_method=bad,
            )

    async def test_both_query_sources_rejected(
        self, search_tools: dict[str, Callable[..., Awaitable[str]]]
    ) -> None:
        with pytest.raises(ValidationError, match="exactly one"):
            await search_tools["search_library"](
                experimental_file="a.mzML",
                spectrum_reference="ptr:spectrum:abc",
                database_file=DB_FILE,
            )

    async def test_neither_query_source_rejected(
        self, search_tools: dict[str, Callable[..., Awaitable[str]]]
    ) -> None:
        with pytest.raises(ValidationError, match="exactly one"):
            await search_tools["search_library"](database_file=DB_FILE)

    async def test_unknown_reference_is_refused_before_dispatch(
        self, search_tools: dict[str, Callable[..., Awaitable[str]]]
    ) -> None:
        import uuid

        with pytest.raises(UnknownReferenceError, match="No live data reference"):
            await search_tools["search_library"](
                spectrum_reference=f"ptr:spectrum:{uuid.uuid4().hex}",
                database_file=DB_FILE,
            )

    async def test_failed_job_returns_traceback(
        self,
        search_tools: dict[str, Callable[..., Awaitable[str]]],
        query_mzml: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def explode(request: SearchRequest) -> None:
            raise RuntimeError("synthetic library generation failure")

        monkeypatch.setattr(search, "_run_scan", explode)

        dispatched = await search_tools["search_library"](
            experimental_file=str(query_mzml),
            database_file=DB_FILE,
        )
        job_id = dispatched.split("`")[1]

        out = await self._poll_until_final(search_tools["check_search_status"], job_id)
        assert out.startswith("❌ **Failed**")
        assert "RuntimeError" in out
        assert "synthetic library generation failure" in out

    @pytest.mark.parametrize("bad", [50, -1, 20000])
    async def test_invalid_chunk_size_raises_validation_error(
        self,
        search_tools: dict[str, Callable[..., Awaitable[str]]],
        bad: int,
    ) -> None:
        with pytest.raises(ValidationError):
            await search_tools["search_library"](
                experimental_file="a.mzML",
                database_file=DB_FILE,
                chunk_size=bad,
            )

    async def test_completed_job_is_expired_after_its_ttl(
        self, search_tools: dict[str, Callable[..., Awaitable[str]]], query_mzml: Path
    ) -> None:
        """A finished job is dropped by the executor once its TTL elapses."""
        dispatched = await search_tools["search_library"](
            experimental_file=str(query_mzml),
            database_file=DB_FILE,
        )
        job_id = dispatched.split("`")[1]
        await self._poll_until_final(search_tools["check_search_status"], job_id)

        # The record is retained for its TTL, then swept.
        assert search._EXECUTOR.status(job_id).is_terminal
        record = search._EXECUTOR._records[job_id]
        record.deadline = 0.0  # already elapsed
        assert search._EXECUTOR.purge_expired() >= 1

        out = await search_tools["check_search_status"](job_id=job_id)
        assert out.startswith("❌ **Failed (not found)**")


# ---------------------------------------------------------------------------
# Truthfulness of the banner
# ---------------------------------------------------------------------------
def test_report_banner_precedes_the_hit_table() -> None:
    """A synthetic-library hit table must say so, in the report itself."""
    report = _scan()
    assert "SYNTHETIC LIBRARY" in report
    assert "NOT A COMPOUND IDENTIFICATION" in report
    assert report.index("SYNTHETIC LIBRARY") < report.index("| Rank | Compound")
    assert "not opened" in report


@pytest.mark.parametrize("method", ["classical", "dreams", "lsm-ms2"])
def test_report_builder_is_well_formed_for_every_scorer(method: str) -> None:
    report = _scan(scoring_method=method)
    assert report.startswith(">")
    assert "SYNTHETIC LIBRARY" in report
