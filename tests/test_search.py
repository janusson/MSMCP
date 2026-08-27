"""Tests for the async job-store spectral library search tools.

Covers the dispatcher/poller contract (``search_library`` records a job in
``_JOB_STORE`` and spawns ``_run_search_task``; ``check_search_status`` polls
the store), the scorer routing (classical vs. foundation-model embeddings),
and the report builder behind the CPU-bound scan.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import ClassVar

import numpy as np
import pytest
from pydantic import ValidationError

from msmcp.models.embeddings import DreaMSEmbedder, LSMMS2Embedder
from msmcp.tools import search
from msmcp.tools.search import (
    _build_mock_database,
    _build_report,
    _build_scorer,
    _cosine,
    _mock_load_experimental,
    _scoring_label,
)
from msmcp.tools.similarity import _cosine as _vector_cosine

EXP_FILE = "experimental/test_spectrum.mzML"
DB_FILE = "library/test_library.db"


# ---------------------------------------------------------------------------
# Search components — mock database, experimental loader, report builder
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

    def test_mock_experimental_loader(self) -> None:
        peaks = _mock_load_experimental(EXP_FILE)
        assert len(peaks) >= 15
        for mz, intensity in peaks:
            assert mz > 0.0
            assert intensity > 0.0

    def test_build_report_well_formed(self) -> None:
        """The report builder renders a complete Markdown report."""
        report = _build_report(EXP_FILE, DB_FILE, "classical")
        assert "## Spectral Library Search Results" in report
        assert "Scoring method: classical (greedy peak matching, ±0.02 Da)" in report
        # The synthetic library is hash-seeded per process, so the number of
        # threshold-passing hits is not deterministic — assert the report is
        # well-formed in either outcome instead.
        assert ("| Rank | Compound" in report) or (
            "No hits passed the significance threshold" in report
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
        assert scorer(self.PEPTIDE, self.RELATED) == pytest.approx(
            _cosine(self.PEPTIDE, self.RELATED)
        )

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
        assert scorer(self.PEPTIDE, self.RELATED) == pytest.approx(expected)

    def test_embedding_scorer_handles_empty_peaks(self) -> None:
        scorer = _build_scorer("dreams")
        assert scorer([], self.PEPTIDE) == 0.0
        assert scorer(self.PEPTIDE, []) == 0.0

    def test_unknown_method_raises(self) -> None:
        with pytest.raises(ValueError, match="dreams, lsm-ms2"):
            _build_scorer("specter2")

    def test_scoring_label(self) -> None:
        assert _scoring_label("classical") == (
            "classical (greedy peak matching, ±0.02 Da)"
        )
        assert _scoring_label("dreams") == (
            "DreaMS deep embedding (1024-d, deterministic fallback)"
        )
        assert _scoring_label("lsm-ms2") == (
            "LSM-MS2 deep embedding (1024-d, deterministic fallback)"
        )


# ---------------------------------------------------------------------------
# Dispatcher → poller round trip against the in-process job store
# ---------------------------------------------------------------------------
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
        self, search_tools: dict[str, Callable[..., Awaitable[str]]]
    ) -> None:
        dispatched = await search_tools["search_library"](
            experimental_file=EXP_FILE,
            database_file=DB_FILE,
        )
        assert dispatched.startswith("## Search Dispatched")
        job_id = dispatched.split("`")[1]  # first code span is the job ID
        assert len(job_id) == 32  # uuid4 hex, no dashes
        uuid.UUID(job_id)  # must be a valid UUID

        report = await self._poll_until_final(
            search_tools["check_search_status"], job_id
        )
        assert "## Spectral Library Search Results" in report
        assert "Scoring method: classical (greedy peak matching, ±0.02 Da)" in report
        assert ("| Rank | Compound" in report) or (
            "No hits passed the significance threshold" in report
        )

    async def test_dreams_embedding_round_trip(
        self, search_tools: dict[str, Callable[..., Awaitable[str]]]
    ) -> None:
        """An embedding-scored search states the model in its report."""
        dispatched = await search_tools["search_library"](
            experimental_file=EXP_FILE,
            database_file=DB_FILE,
            scoring_method="dreams",
        )
        job_id = dispatched.split("`")[1]
        report = await self._poll_until_final(
            search_tools["check_search_status"], job_id
        )
        assert "## Spectral Library Search Results" in report
        assert (
            "Scoring method: DreaMS deep embedding (1024-d, deterministic fallback)"
            in report
        )
        assert ("| Rank | Compound" in report) or (
            "No hits passed the significance threshold" in report
        )

    async def test_unknown_job_id(
        self, search_tools: dict[str, Callable[..., Awaitable[str]]]
    ) -> None:
        out = await search_tools["check_search_status"](job_id=uuid.uuid4().hex)
        assert out.startswith("❓ **Unknown Job**")
        assert "No search job found" in out

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
                experimental_file=EXP_FILE,
                database_file=DB_FILE,
                scoring_method=bad,
            )

    async def test_failed_job_returns_traceback(
        self,
        search_tools: dict[str, Callable[..., Awaitable[str]]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def explode(
            experimental_file: str,
            database_file: str,
            scoring_method: str,
            chunk_size: int = 2000,
        ) -> str:
            raise RuntimeError("synthetic library generation failure")

        monkeypatch.setattr(search, "_build_report", explode)

        dispatched = await search_tools["search_library"](
            experimental_file=EXP_FILE,
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
                experimental_file=EXP_FILE,
                database_file=DB_FILE,
                chunk_size=bad,
            )

    async def test_schedule_cleanup_expires_job(self) -> None:
        """A finished job is removed from the store once the TTL elapses."""
        job_id = uuid.uuid4().hex
        search._JOB_STORE[job_id] = search.SearchJob(
            job_id=job_id,
            experimental_file=EXP_FILE,
            database_file=DB_FILE,
            scoring_method="classical",
        )

        await search._schedule_cleanup(job_id, delay_sec=0)
        assert job_id not in search._JOB_STORE

    async def test_run_search_task_schedules_cleanup(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The task's ``finally`` spawns a TTL cleanup for the finished job."""
        cleaned: list[str] = []

        async def fake_cleanup(job_id: str, delay_sec: int = 3600) -> None:
            cleaned.append(job_id)

        monkeypatch.setattr(search, "_schedule_cleanup", fake_cleanup)
        job_id = uuid.uuid4().hex
        search._JOB_STORE[job_id] = search.SearchJob(
            job_id=job_id,
            experimental_file=EXP_FILE,
            database_file=DB_FILE,
            scoring_method="classical",
        )

        await search._run_search_task(job_id, EXP_FILE, DB_FILE, "classical")
        await asyncio.sleep(0)  # let the spawned cleanup task run
        assert cleaned == [job_id]
        assert search._JOB_STORE[job_id].status == "completed"
