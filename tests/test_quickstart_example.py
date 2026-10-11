"""The README's "Try it in 60 seconds" example, executed exactly as written.

The README tells a reader to search ``examples/caffeine_spectrum.mgf`` against
``examples/caffeine_library.msp`` and promises a REAL LIBRARY banner with
caffeine ranked first.  A quickstart that has quietly stopped working is worse
than no quickstart, so this drives that flow through the registered tools — the
same callables a host invokes — and asserts the promised outcome.  If the
fixtures move, their peak data changes, or the tool contract drifts, this fails.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from msmcp import ingest, security
from msmcp.state import store as reference_store
from msmcp.tools import io as msmcp_io
from msmcp.tools import qc

REPO_ROOT = Path(__file__).resolve().parents[1]
LIBRARY = "examples/caffeine_library.msp"
SPECTRUM = "examples/caffeine_spectrum.mgf"


@pytest.fixture()
def example_root(monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Confine the tools to the repository root and run the test from there.

    The README's paths are relative, so the working directory must be the
    repository root for them to resolve; the security policy is set to that same
    root, mirroring how the server is configured once before it starts serving.
    """
    policy = security.SecurityPolicy(allowed_root=REPO_ROOT)
    monkeypatch.setattr(msmcp_io, "DEFAULT_POLICY", policy)
    monkeypatch.setattr(qc, "DEFAULT_POLICY", policy)
    monkeypatch.setattr(ingest, "DEFAULT_POLICY", policy)
    monkeypatch.setattr(security, "DEFAULT_POLICY", policy)
    monkeypatch.chdir(REPO_ROOT)
    reference_store.reset_store()
    yield REPO_ROOT
    reference_store.reset_store()


def test_the_documented_example_files_are_shipped() -> None:
    """The README points at real files; a missing fixture is the rot to catch."""
    assert (REPO_ROOT / LIBRARY).is_file(), f"{LIBRARY} is not shipped"
    assert (REPO_ROOT / SPECTRUM).is_file(), f"{SPECTRUM} is not shipped"


async def test_try_it_in_60_seconds_reproduces_the_documented_result(
    io_tools: dict[str, Callable[..., Any]],
    search_tools: dict[str, Callable[..., Awaitable[str]]],
    example_root: Path,
) -> None:
    """Load the worked example, search it, and get caffeine — as advertised."""
    # 1. Load the worked-example spectrum, relative path and all.
    loaded = io_tools["load_spectrum"](SPECTRUM, spectrum_index=0)
    assert loaded.reference.startswith("ptr:spectrum:")
    assert loaded.format == "MGF"
    assert loaded.ms_level == 2
    assert loaded.precursor_mz == pytest.approx(195.0877)
    assert loaded.n_peaks == 5

    # 2. Search it against the worked-example library — a real MSP read.
    dispatched = await search_tools["search_library"](
        spectrum_reference=loaded.reference,
        database_file=LIBRARY,
        instrument_class="orbitrap",
    )
    assert dispatched.startswith("## Search Dispatched")
    assert "read from disk" in dispatched
    job_id = dispatched.split("`")[1]

    # 3. Poll for the report the README shows.
    report = await _poll(search_tools["check_search_status"], job_id)
    assert "❌" not in report, report

    # The real-library banner, ahead of the hit table, and not the synthetic one.
    assert "REAL LIBRARY" in report
    assert "SYNTHETIC LIBRARY" not in report
    assert LIBRARY in report
    assert report.index("REAL LIBRARY") < report.index("| Rank | Compound")
    # Caffeine, the query's known true positive, ranked first.
    assert "Caffeine" in report
    assert "1.0000" in report

    # 4. Release the reference, as the README's walkthrough does.
    assert "Released" in io_tools["release_reference"](loaded.reference)


async def _poll(
    check: Callable[..., Awaitable[str]], job_id: str, timeout: float = 180.0
) -> str:
    """Poll until the job returns a report or a failure."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        out = await check(job_id=job_id)
        if "## Spectral Library Search Results" in out or out.startswith(
            "❌ **Failed**"
        ):
            return out
        await asyncio.sleep(0.2)
    pytest.fail(f"job {job_id} never finished")
