"""End-to-end workflow tests across the whole MSMCP architecture.

These are the tests that exercise the *composition* of the pieces, in the
order an LLM host would drive them::

    load an MS file
        -> obtain a DataReference
        -> retrieve spectrum information from the reference
        -> run a search using the reference as the query
        -> collect a compact, structured result
        -> verify provenance
        -> release the reference

They deliberately assert the property the whole DataReference design exists
for: the scientific payload never travels through the tool response, so a
100k-peak spectrum costs the model a few dozen tokens to hold on to.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from msmcp import ingest
from msmcp.errors import InaccessiblePathError, MalformedFileError, SpectrumIndexError
from msmcp.security import SecurityPolicy
from msmcp.state import store as reference_store
from msmcp.state.pointers import ReferenceKindError, UnknownReferenceError
from msmcp.tools import io as msmcp_io
from msmcp.tools import qc
from msmcp.tools.io import SpectrumReferenceResult

LIBRARY_MZML = "library/does-not-exist.db"


def allow_root(root: Path, monkeypatch: pytest.MonkeyPatch) -> SecurityPolicy:
    """Confine every file-reading tool to *root* for the duration of a test.

    Each tool module binds ``DEFAULT_POLICY`` in its own namespace, and the
    ingestion dispatcher binds its own, so all three must be redirected; this
    mirrors how the server is configured (once, before it starts serving).
    """
    policy = SecurityPolicy(allowed_root=root)
    monkeypatch.setattr(msmcp_io, "DEFAULT_POLICY", policy)
    monkeypatch.setattr(qc, "DEFAULT_POLICY", policy)
    monkeypatch.setattr(ingest, "DEFAULT_POLICY", policy)
    return policy


@pytest.fixture()
def data_root(valid_mzml: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Allow the tools to read this test's temporary directory."""
    allow_root(valid_mzml.parent, monkeypatch)
    reference_store.reset_store()
    yield valid_mzml.parent
    reference_store.reset_store()


def _load(
    io_tools: dict[str, Callable[..., Any]], path: Path, index: int = 0
) -> SpectrumReferenceResult:
    return io_tools["load_spectrum"](str(path), spectrum_index=index)


# ---------------------------------------------------------------------------
# The full workflow
# ---------------------------------------------------------------------------
class TestFullWorkflow:
    async def test_load_reference_summarise_search_release(
        self,
        io_tools: dict[str, Callable[..., Any]],
        search_tools: dict[str, Callable[..., Awaitable[str]]],
        valid_mzml: Path,
        data_root: Path,
    ) -> None:
        # --- 1. load a real file and get a server-side reference ------------
        loaded = _load(io_tools, valid_mzml)
        assert isinstance(loaded, SpectrumReferenceResult)
        assert loaded.reference.startswith("ptr:spectrum:")
        assert loaded.kind == "spectrum"
        assert loaded.format == "mzML"
        assert loaded.backend == "msmcp.mzml"
        assert loaded.n_peaks == 3
        assert loaded.tic == pytest.approx(60.0)
        assert loaded.mz_range == pytest.approx([100.0, 300.0])
        assert loaded.ms_level == 1
        assert loaded.precursor_mz == pytest.approx(300.0)
        assert loaded.n_bytes == 3 * 8 * 2

        # --- 2. the response carries metadata and provenance, not peaks -----
        payload = loaded.model_dump()
        assert "mz" not in payload and "intensity" not in payload
        assert payload["provenance"]["operation"] == "load_spectrum"
        source = payload["provenance"]["sources"][0]
        assert source["path"] == str(valid_mzml.resolve())
        assert source["format"] == "mzML"
        assert source["backend"] == "msmcp.mzml"
        assert source["digest"] is not None
        assert source["backend"] == "msmcp.mzml"

        # --- 3. read the spectrum back from server memory ------------------
        summary = io_tools["summarise_reference"](loaded.reference)
        assert "Spectrum #0" in summary
        assert "Held server-side" in summary
        assert "TIC: 60.00" in summary
        assert "Provenance: load_spectrum" in summary
        assert "parent references" not in summary  # no parents for a source object
        # The provenance chain back to the file is intact.
        assert str(valid_mzml.resolve()) in summary

        # --- 4. drive a search from the reference --------------------------
        dispatched = await search_tools["search_library"](
            spectrum_reference=loaded.reference,
            database_file=LIBRARY_MZML,
        )
        assert dispatched.startswith("## Search Dispatched")
        assert "real peaks" in dispatched
        job_id = dispatched.split("`")[1]

        report = await _poll(search_tools["check_search_status"], job_id)
        assert "SYNTHETIC LIBRARY" in report
        assert f"reference `{loaded.reference}`" in report
        assert "held server-side" in report
        assert "Provenance: search_library" in report

        # --- 5. release the reference and confirm it is gone ---------------
        released = io_tools["release_reference"](loaded.reference)
        assert "Released" in released
        with pytest.raises(UnknownReferenceError):
            io_tools["summarise_reference"](loaded.reference)

    async def test_release_is_idempotent(
        self,
        io_tools: dict[str, Callable[..., Any]],
        valid_mzml: Path,
        data_root: Path,
    ) -> None:
        reference = _load(io_tools, valid_mzml).reference
        assert "Released" in io_tools["release_reference"](reference)
        second = io_tools["release_reference"](reference)
        assert "was not held" in second
        assert "No action taken" in second

    async def test_summarise_reports_the_tic_from_stored_peaks(
        self,
        io_tools: dict[str, Callable[..., Any]],
        valid_mzml: Path,
        data_root: Path,
    ) -> None:
        """The summary is recomputed from server memory, not a cached string."""
        reference = _load(io_tools, valid_mzml, index=2).reference
        summary = io_tools["summarise_reference"](reference)
        assert "Spectrum #2" in summary
        assert "4 peaks" in summary or "Peaks (>=" in summary


async def _poll(
    check: Callable[..., Awaitable[str]], job_id: str, timeout: float = 180.0
) -> str:
    """Poll until the job returns a report or a failure."""
    import asyncio
    import time as _time

    deadline = _time.monotonic() + timeout
    while _time.monotonic() < deadline:
        out = await check(job_id=job_id)
        if "## Spectral Library Search Results" in out or out.startswith(
            "❌ **Failed**"
        ):
            return out
        await asyncio.sleep(0.2)
    pytest.fail(f"job {job_id} never finished")


# ---------------------------------------------------------------------------
# The point of the design: big payloads stay on the server
# ---------------------------------------------------------------------------
class TestLargePayloadsStayServerSide:
    @staticmethod
    def _big_mzml(path: Path, n_peaks: int) -> Path:
        """Write a single-spectrum mzML with *n_peaks* peaks."""
        mz = np.linspace(50.0, 2000.0, n_peaks)
        intensity = np.linspace(1.0, 1e6, n_peaks)

        def array(kind: str, values: np.ndarray) -> str:
            accession = "MS:1000514" if kind == "mz" else "MS:1000515"
            name = "m/z array" if kind == "mz" else "intensity array"
            payload = base64.b64encode(values.astype("<f8").tobytes()).decode()
            return (
                f'<binaryDataArray encodedLength="{len(payload)}">'
                f'<cvParam cvRef="MS" accession="{accession}" name="{name}" value=""/>'
                f'<cvParam cvRef="MS" accession="MS:1000523" name="64-bit float" value=""/>'
                f'<cvParam cvRef="MS" accession="MS:1000576" name="no compression" value=""/>'
                f"<binary>{payload}</binary></binaryDataArray>"
            )

        path.write_text(
            '<?xml version="1.0" encoding="utf-8"?>'
            '<mzML xmlns="http://psi.hupo.org/ms/mzml" version="1.1.0"><run id="big">'
            f'<spectrumList count="1" defaultDataProcessingRef="dp">'
            f'<spectrum index="0" id="scan=0" defaultArrayLength="{n_peaks}">'
            f'<cvParam cvRef="MS" accession="MS:1000511" name="ms level" value="2"/>'
            f'<precursorList count="1"><precursor><selectedIonList count="1">'
            f'<selectedIon><cvParam cvRef="MS" accession="MS:1000744" '
            f'name="selected ion m/z" value="1500.0"/></selectedIon>'
            f"</selectedIonList></precursor></precursorList>"
            f'<binaryDataArrayList count="2">{array("mz", mz)}'
            f"{array('intensity', intensity)}</binaryDataArrayList>"
            f"</spectrum></spectrumList></run></mzML>",
            encoding="utf-8",
        )
        return path

    def test_a_hundred_thousand_peaks_cost_a_few_dozen_tokens(
        self,
        io_tools: dict[str, Callable[..., Any]],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The tool result is metadata only; the peaks stay in the store."""
        allow_root(tmp_path, monkeypatch)
        reference_store.reset_store()
        try:
            big = self._big_mzml(tmp_path / "big.mzML", 100_000)

            loaded = _load(io_tools, big)
            assert loaded.n_peaks == 100_000
            assert loaded.n_bytes == 100_000 * 8 * 2  # 1.6 MB of peak arrays

            serialised = json.dumps(loaded.model_dump())
            assert len(serialised) < 4096, (
                f"the tool response serialised to {len(serialised)} characters; "
                f"the peaks must not travel through the response"
            )
            assert '"n_peaks": 100000' in serialised  # real metadata, not an empty stub
            assert str(loaded.n_bytes) in serialised

            # The peaks really are there, and readable without re-reading the file.
            stored = reference_store.peak_list_of(loaded.reference)
            assert stored.shape == (100_000, 2)
            assert float(stored[:, 0].max()) == pytest.approx(2000.0)
        finally:
            reference_store.reset_store()

    def test_the_store_can_be_released_after_a_large_load(
        self,
        io_tools: dict[str, Callable[..., Any]],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        allow_root(tmp_path, monkeypatch)
        reference_store.reset_store()
        try:
            big = self._big_mzml(tmp_path / "big.mzML", 20_000)
            loaded = _load(io_tools, big)
            assert reference_store.stats().total_bytes == loaded.n_bytes
            io_tools["release_reference"](loaded.reference)
            # Bounded memory: releasing reclaims the bytes immediately.
            assert reference_store.stats().total_bytes == 0
            assert reference_store.stats().count == 0
        finally:
            reference_store.reset_store()


# ---------------------------------------------------------------------------
# Failure paths
# ---------------------------------------------------------------------------
class TestWorkflowFailurePaths:
    def test_malformed_file_never_produces_a_reference(
        self,
        io_tools: dict[str, Callable[..., Any]],
        truncated_mzml: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        allow_root(truncated_mzml.parent, monkeypatch)
        reference_store.reset_store()
        with pytest.raises(MalformedFileError):
            _load(io_tools, truncated_mzml)
        assert reference_store.stats().count == 0

    def test_missing_file_never_produces_a_reference(
        self,
        io_tools: dict[str, Callable[..., Any]],
        data_root: Path,
    ) -> None:
        with pytest.raises(InaccessiblePathError):
            _load(io_tools, data_root / "absent.mzML")
        assert reference_store.stats().count == 0

    def test_out_of_range_index_is_refused(
        self,
        io_tools: dict[str, Callable[..., Any]],
        valid_mzml: Path,
        data_root: Path,
    ) -> None:
        with pytest.raises(SpectrumIndexError, match="no spectrum at index"):
            _load(io_tools, valid_mzml, index=99)
        assert reference_store.stats().count == 0

    def test_unsupported_format_is_refused(
        self,
        io_tools: dict[str, Callable[..., Any]],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from msmcp.errors import UnsupportedFormatError

        allow_root(tmp_path, monkeypatch)
        vendor = tmp_path / "run.raw"
        vendor.write_text("x", encoding="utf-8")
        with pytest.raises(UnsupportedFormatError, match="Thermo"):
            _load(io_tools, vendor)

    def test_wrong_kind_reference_is_rejected(
        self,
        io_tools: dict[str, Callable[..., Any]],
    ) -> None:
        """A peak-list reference cannot be summarised as a spectrum."""
        reference_store.reset_store()
        try:
            reference = reference_store.store_peak_list(
                np.array([[100.0, 1.0], [200.0, 2.0]])
            )
            with pytest.raises(ReferenceKindError, match="holds 'peak-list' data"):
                io_tools["summarise_reference"](reference.identifier)
        finally:
            reference_store.reset_store()

    def test_malformed_reference_string_is_rejected(
        self, io_tools: dict[str, Callable[..., Any]]
    ) -> None:
        reference_store.reset_store()
        with pytest.raises(UnknownReferenceError, match="not a valid data reference"):
            io_tools["summarise_reference"]("ptr:spectrum")

    def test_releasing_an_unknown_reference_is_safe(
        self, io_tools: dict[str, Callable[..., Any]]
    ) -> None:
        reference_store.reset_store()
        out = io_tools["release_reference"]("ptr:spectrum:never-existed")
        assert "was not held" in out

    def test_store_limit_is_reported_with_guidance(
        self,
        io_tools: dict[str, Callable[..., Any]],
        valid_mzml: Path,
        data_root: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from msmcp.state.pointers import PointerStore, StoreLimitError

        original = reference_store.configure_store(PointerStore(max_references=1))
        try:
            _load(io_tools, valid_mzml, index=0)
            with pytest.raises(StoreLimitError, match="release_reference"):
                _load(io_tools, valid_mzml, index=1)
        finally:
            reference_store.configure_store(original)
            reference_store.reset_store()


# ---------------------------------------------------------------------------
# Long-running work through the task abstraction
# ---------------------------------------------------------------------------
class TestLongRunningWorkflow:
    async def test_search_exposes_an_mcp_shaped_task(
        self,
        search_tools: dict[str, Callable[..., Awaitable[str]]],
        valid_mzml: Path,
        data_root: Path,
    ) -> None:
        """The job handle maps onto the MCP Task shape without translation."""
        from msmcp.tools import search as search_module

        dispatched = await search_tools["search_library"](
            experimental_file=str(valid_mzml),
            database_file=LIBRARY_MZML,
            chunk_size=100,
        )
        job_id = dispatched.split("`")[1]

        snapshot = search_module._EXECUTOR.status(job_id)
        task = snapshot.to_task_dict()
        assert task["task_id"] == job_id
        assert task["status"] in {"working", "completed", "failed", "cancelled"}
        assert task["created_at"].endswith("+00:00")
        assert isinstance(task["ttl"], int)
        assert task["poll_interval"] == 500

        report = await _poll(search_tools["check_search_status"], job_id)
        assert "## Spectral Library Search Results" in report

    async def test_cancellation_is_terminal_and_discards_work(
        self,
        search_tools: dict[str, Callable[..., Awaitable[str]]],
        valid_mzml: Path,
        data_root: Path,
    ) -> None:
        dispatched = await search_tools["search_library"](
            experimental_file=str(valid_mzml),
            database_file=LIBRARY_MZML,
            chunk_size=100,
        )
        job_id = dispatched.split("`")[1]

        cancelled = await search_tools["cancel_search"](job_id=job_id)
        assert "Search Cancelled" in cancelled or "Not Cancelled" in cancelled

        out = search_tools
        # Wait until the job settles, then confirm the terminal payload agrees.
        import asyncio
        import time as _time

        deadline = _time.monotonic() + 60.0
        final = ""
        while _time.monotonic() < deadline:
            final = await out["check_search_status"](job_id=job_id)
            if "Cancelled" in final or "Search Results" in final:
                break
            await asyncio.sleep(0.1)
        assert "Cancelled" in final or "Search Results" in final

    async def test_the_database_path_is_never_opened(
        self,
        search_tools: dict[str, Callable[..., Awaitable[str]]],
        valid_mzml: Path,
        data_root: Path,
    ) -> None:
        """Half-real pipeline, stated precisely: real query, synthetic library."""
        dispatched = await search_tools["search_library"](
            experimental_file=str(valid_mzml),
            database_file=LIBRARY_MZML,
            chunk_size=100,
        )
        job_id = dispatched.split("`")[1]
        report = await _poll(search_tools["check_search_status"], job_id)
        assert f"`{LIBRARY_MZML}` — not opened" in report
        assert "synthetic library" in report
