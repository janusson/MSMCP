"""Tests for the Prefect-orchestrated spectral library search tools."""

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
    _build_scorer,
    _cosine,
    _generate_library_task,
    _load_experimental_spectrum_task,
    _mock_load_experimental,
    _scoring_label,
)
from msmcp.tools.similarity import _cosine as _vector_cosine

EXP_FILE = "experimental/test_spectrum.mzML"
DB_FILE = "library/test_library.db"


# ---------------------------------------------------------------------------
# Flow internals — plain functions behind the Prefect tasks
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

    def test_task_fn_matches_plain_helpers(self) -> None:
        """Task ``.fn`` copies wrap the plain helpers (observable lineage)."""
        conn = _generate_library_task.fn(n_spectra=10, seed=7)  # type: ignore[attr-defined]
        try:
            assert conn.execute("SELECT COUNT(*) FROM spectra").fetchone()[0] == 10
        finally:
            conn.close()
        peaks = _load_experimental_spectrum_task.fn(EXP_FILE, 42)  # type: ignore[attr-defined]
        assert len(peaks) >= 15


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
        assert _scoring_label("dreams") == "DreaMS deep embedding (1024-d)"
        assert _scoring_label("lsm-ms2") == "LSM-MS2 deep embedding (1024-d)"


# ---------------------------------------------------------------------------
# Dispatcher → poller round trip against the Prefect API
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
        # The dispatcher must advertise durable Prefect orchestration.
        assert "Prefect" in dispatched
        job_id = dispatched.split("`")[1]  # first code span is the job ID
        uuid.UUID(job_id)  # must be a valid flow run ID

        report = await self._poll_until_final(
            search_tools["check_search_status"], job_id
        )
        assert "## Spectral Library Search Results" in report
        assert "Scoring method: classical (greedy peak matching, ±0.02 Da)" in report
        # The synthetic library is hash-seeded per process, so the number of
        # threshold-passing hits is not deterministic — assert the report is
        # well-formed in either outcome instead.
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
        assert "Scoring method: DreaMS deep embedding (1024-d)" in report
        assert ("| Rank | Compound" in report) or (
            "No hits passed the significance threshold" in report
        )

    async def test_unknown_job_id(
        self, search_tools: dict[str, Callable[..., Awaitable[str]]]
    ) -> None:
        out = await search_tools["check_search_status"](job_id=str(uuid.uuid4()))
        assert out.startswith("❓ **Unknown Job**")
        assert "No Prefect flow run found" in out

    async def test_malformed_job_id(
        self, search_tools: dict[str, Callable[..., Awaitable[str]]]
    ) -> None:
        out = await search_tools["check_search_status"](job_id="not-a-uuid")
        assert out.startswith("❓ **Unknown Job**")
        assert "not a valid Prefect flow run ID" in out

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

    async def test_failed_flow_returns_traceback(
        self,
        search_tools: dict[str, Callable[..., Awaitable[str]]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def explode(n_spectra: int, seed: int) -> None:
            raise RuntimeError("synthetic library generation failure")

        monkeypatch.setattr(search, "_generate_library_task", explode)

        dispatched = await search_tools["search_library"](
            experimental_file=EXP_FILE,
            database_file=DB_FILE,
        )
        job_id = dispatched.split("`")[1]

        out = await self._poll_until_final(search_tools["check_search_status"], job_id)
        assert out.startswith("❌ **Failed**")
        assert "RuntimeError" in out
        assert "synthetic library generation failure" in out
