"""An unreadable library path must fail closed, not answer from a fabrication.

``search_library`` used to substitute a synthetic library whenever no reader
understood the path.  Deterministic seeding made that reproducible without
making it correct: a user pointing at a real library in an unsupported format
received plausible hits from fabricated spectra, with only a prose banner to
say so.

The fallback is now opt-in, under the same rule as the mock embedding backends
(``MSMCP_EMBEDDING_BACKEND=mock``): production refuses, and the demonstration
library is reachable only by asking for it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from msmcp.errors import UnsupportedFormatError
from msmcp.tools.search import (
    ENV_LIBRARY_FALLBACK,
    LIBRARY_FALLBACK_SYNTHETIC,
    SearchRequest,
    _run_scan,
)

QUERY_PEAKS = ((110.0713, 40.0), (120.0808, 100.0), (138.0662, 420.0))


def _request(database_file: str) -> SearchRequest:
    return SearchRequest(
        database_file=database_file,
        experimental_peaks=QUERY_PEAKS,
        experimental_file="experimental/run42.mgf",
    )


def test_an_unreadable_library_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """The default is a refusal, not a silently fabricated library."""
    monkeypatch.delenv(ENV_LIBRARY_FALLBACK, raising=False)

    with pytest.raises(UnsupportedFormatError) as refusal:
        _run_scan(_request("libraries/metabolomics.txt"))

    message = str(refusal.value)
    assert "metabolomics.txt" in message
    assert "not a spectral library MSMCP can read" in message
    # The reader has to be able to fix it without reading the source.
    assert ".msp" in message and ".mgf" in message and ".sqlite" in message
    assert f"{ENV_LIBRARY_FALLBACK}={LIBRARY_FALLBACK_SYNTHETIC}" in message


@pytest.mark.parametrize("value", ["synth", "yes", "1", "", "SYNTHETIC "])
def test_only_the_exact_flag_enables_the_demo_library(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    """A near-miss does not turn a fabricated library on by accident."""
    monkeypatch.setenv(ENV_LIBRARY_FALLBACK, value)

    expected = value.strip().lower() == LIBRARY_FALLBACK_SYNTHETIC
    if expected:
        report = _run_scan(_request("libraries/metabolomics.txt")).report
        assert "SYNTHETIC LIBRARY" in report
    else:
        with pytest.raises(UnsupportedFormatError):
            _run_scan(_request("libraries/metabolomics.txt"))


def test_demo_mode_is_labelled_in_the_report_and_provenance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Opting in is allowed; passing the result off as real is not."""
    monkeypatch.setenv(ENV_LIBRARY_FALLBACK, LIBRARY_FALLBACK_SYNTHETIC)

    outcome = _run_scan(_request("libraries/metabolomics.txt"))

    assert "SYNTHETIC LIBRARY" in outcome.report
    assert "NOT A COMPOUND IDENTIFICATION" in outcome.report
    parameters = outcome.provenance.to_dict()["parameters"]
    assert parameters["library_synthetic"] is True


async def test_the_tool_refuses_with_a_readable_reason(
    monkeypatch: pytest.MonkeyPatch, search_tools
) -> None:
    """The refusal reaches the host with its reason, not as an opaque crash.

    The MCP SDK replaces a crashing exception's text with a generic message, so
    the refusal is raised deliberately; without that the caller would see only
    "Error executing tool search_library" and never learn the fix.
    """
    from mcp.server.mcpserver.exceptions import ToolError

    monkeypatch.delenv(ENV_LIBRARY_FALLBACK, raising=False)

    with pytest.raises(ToolError) as refusal:
        await search_tools["search_library"](
            spectrum_reference="ptr:spectrum:000000000000000000000000",
            database_file="libraries/metabolomics.txt",
        )

    assert f"{ENV_LIBRARY_FALLBACK}={LIBRARY_FALLBACK_SYNTHETIC}" in str(refusal.value)


def test_a_supported_library_is_unaffected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The refusal is about unreadable paths only; a real MSP library still runs."""
    from msmcp import security

    monkeypatch.delenv(ENV_LIBRARY_FALLBACK, raising=False)
    policy = security.SecurityPolicy(allowed_root=tmp_path)
    monkeypatch.setattr(security, "DEFAULT_POLICY", policy)

    library = tmp_path / "library.msp"
    library.write_text(
        "Name: Caffeine\nPrecursorMZ: 195.0877\nIon_mode: P\nNum Peaks: 3\n"
        "110.0713 40.0\n120.0808 100.0\n138.0662 420.0\n",
        encoding="utf-8",
    )

    report = _run_scan(_request(str(library))).report

    assert "REAL LIBRARY" in report
    assert "SYNTHETIC" not in report
