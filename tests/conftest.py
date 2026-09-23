"""Shared fixtures for the MSMCP test suite.

The MCP tools are defined as closures inside each tool module's
``register_tools(mcp)`` function.  The test suite registers them against a
minimal stand-in for the current ``MCPServer`` (the real SDK only receives a
``tool()`` decorator call) and exposes the captured callables as fixtures.
"""

from __future__ import annotations

import base64
import gzip
import os
import zlib
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import numpy as np
import pytest

# Pin embedding backends to the deterministic *mock* embedders for hermetic
# tests (MSMCP_EMBEDDING_BACKEND=mock is the explicit test/dev-only flag): no
# torch / DreaMS imports, no weight downloads, no network.  Adapter tests
# that exercise the real-inference pipeline inject stub models explicitly.
os.environ["MSMCP_EMBEDDING_BACKEND"] = "mock"

from msmcp.tools import chem, io, qc, search, similarity


class FakeMCP[T]:
    """Minimal stand-in for ``MCPServer`` that captures registered tools.

    ``tool()`` mirrors the real SDK's decorator signature loosely: it accepts
    the display metadata the tools pass (``title``, ``annotations``, ...) and
    ignores it, because the tests exercise the raw callables rather than the
    protocol surface.  Wire-level assertions live in ``test_tool_schemas.py``,
    which builds the real ``MCPServer``.
    """

    def __init__(self) -> None:
        self.tools: dict[str, Callable[..., T]] = {}

    def tool(self, **kwargs: object) -> Callable[[Callable[..., T]], Callable[..., T]]:
        """Return a decorator that records *fn* and returns it unchanged."""

        def decorator(fn: Callable[..., T]) -> Callable[..., T]:
            self.tools[fn.__name__] = fn
            return fn

        return decorator


@pytest.fixture(scope="session")
def chem_tools() -> dict[str, Callable[..., str]]:
    """Cheminformatics tools keyed by name (``predict_adduct_offset``, ...)."""
    mcp = FakeMCP[str]()
    chem.register_tools(mcp)
    return mcp.tools


@pytest.fixture(scope="session")
def sim_tools() -> dict[str, Callable[..., str]]:
    """Similarity/validation tools keyed by name (``validate_precursor``, ...)."""
    mcp = FakeMCP[str]()
    similarity.register_tools(mcp)
    return mcp.tools


@pytest.fixture(scope="session")
def search_tools() -> dict[str, Callable[..., Awaitable[str]]]:
    """Library-search tools keyed by name (``search_library``, ...)."""
    mcp = FakeMCP[Awaitable[str]]()
    search.register_tools(mcp)
    return mcp.tools


@pytest.fixture(scope="session")
def io_tools() -> dict[str, Callable[..., str]]:
    """I/O tools keyed by name (``load_mzml_summary``)."""
    mcp = FakeMCP[str]()
    io.register_tools(mcp)
    return mcp.tools


@pytest.fixture(scope="session")
def qc_tools() -> dict[str, Callable[..., str]]:
    """QC tools keyed by name (``generate_qc_summary``)."""
    mcp = FakeMCP[str]()
    qc.register_tools(mcp)
    return mcp.tools


# ---------------------------------------------------------------------------
# mzML fixtures — a valid file and a truncated/malformed file
# ---------------------------------------------------------------------------
_NS = "http://psi.hupo.org/ms/mzml"


def _b64_floats(values: list[float], *, compress: bool = False) -> str:
    raw = np.asarray(values, dtype="<f8").tobytes()
    if compress:
        raw = zlib.compress(raw)
    return base64.b64encode(raw).decode()


def _binary_array(kind: str, values: list[float]) -> str:
    if kind == "mz":
        acc, name = "MS:1000514", "m/z array"
    else:
        acc, name = "MS:1000515", "intensity array"
    payload = _b64_floats(values)
    return (
        f'<binaryDataArray encodedLength="{len(payload)}">'
        f'<cvParam cvRef="MS" accession="{acc}" name="{name}" value=""/>'
        f'<cvParam cvRef="MS" accession="MS:1000523" name="64-bit float" value=""/>'
        f'<cvParam cvRef="MS" accession="MS:1000576" name="no compression" value=""/>'
        f"<binary>{payload}</binary>"
        f"</binaryDataArray>"
    )


def _mzml_xml(spectra: list[dict[str, Any]]) -> str:
    """Render a minimal, valid mzML document for the supplied spectra."""
    spectrum_xml: list[str] = []
    for i, spec in enumerate(spectra):
        index = int(spec.get("index", i))
        ms_level = int(spec.get("ms_level", 1))
        rt = float(spec.get("rt_seconds", i))
        precursor = spec.get("precursor_mz")

        precursor_xml = ""
        if precursor is not None:
            precursor_xml = (
                f'<precursorList count="1"><precursor>'
                f'<selectedIonList count="1"><selectedIon>'
                f'<cvParam cvRef="MS" accession="MS:1000744" '
                f'name="selected ion m/z" value="{precursor}"/>'
                f"</selectedIon></selectedIonList></precursor></precursorList>"
            )

        spectrum_xml.append(
            f'<spectrum index="{index}" id="scan={index}" '
            f'defaultArrayLength="{len(spec["mz"])}">'
            f'<cvParam cvRef="MS" accession="MS:1000511" name="ms level" '
            f'value="{ms_level}"/>'
            f'<scanList count="1"><scan>'
            f'<cvParam cvRef="MS" accession="MS:1000016" name="scan start time" '
            f'value="{rt}" unitAccession="MS:1000038" unitName="second"/>'
            f"</scan></scanList>"
            f"{precursor_xml}"
            f'<binaryDataArrayList count="2">'
            f"{_binary_array('mz', list(spec['mz']))}"
            f"{_binary_array('intensity', list(spec['intensity']))}"
            f"</binaryDataArrayList>"
            f"</spectrum>"
        )

    return (
        f'<?xml version="1.0" encoding="utf-8"?>'
        f'<mzML xmlns="{_NS}" version="1.1.0">'
        f'<run id="test"/>'
        f'<spectrumList count="{len(spectra)}" defaultDataProcessingRef="dp">'
        f"{''.join(spectrum_xml)}"
        f"</spectrumList>"
        f"</mzML>"
    )


@pytest.fixture()
def valid_mzml(tmp_path: Path) -> Path:
    """A well-formed 3-spectrum mzML file with known TIC and peak counts."""
    path = tmp_path / "valid.mzML"
    spectra: list[dict[str, Any]] = [
        {
            "mz": [100.0, 200.0, 300.0],
            "intensity": [10.0, 20.0, 30.0],
            "precursor_mz": 300.0,
        },
        {"mz": [101.0, 201.0], "intensity": [5.0, 5.0]},
        {"mz": [102.0, 202.0, 302.0, 402.0], "intensity": [1.0, 2.0, 3.0, 4.0]},
    ]
    path.write_text(_mzml_xml(spectra), encoding="utf-8")
    return path


@pytest.fixture()
def truncated_mzml(tmp_path: Path) -> Path:
    """An mzML file cut off mid-document so the XML cannot parse."""
    path = tmp_path / "truncated.mzML"
    full = _mzml_xml([{"mz": [100.0, 200.0], "intensity": [10.0, 20.0]}])
    path.write_text(full[: len(full) // 2], encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# MGF fixtures — a valid file plus the malformed cases the reader must reject
# ---------------------------------------------------------------------------
_GOOD_MGF = """# MGF fixture written by the MSMCP test suite
BEGIN IONS
TITLE=caffeine-like
PEPMASS=194.0804 1234.5
CHARGE=1+
RTINSECONDS=120.0
MSLEVEL=2
110.0713 40.0
120.0808 100.0
136.0757 60.0
END IONS
BEGIN IONS
TITLE=glucose-like
PEPMASS=180.0634
RTINMINUTES=3.5
100.0 5.0
200.0 7.5
END IONS
"""


@pytest.fixture()
def valid_mgf(tmp_path: Path) -> Path:
    """A well-formed 2-spectrum MGF file with known peaks and metadata."""
    path = tmp_path / "valid.mgf"
    path.write_text(_GOOD_MGF, encoding="utf-8")
    return path


@pytest.fixture()
def gzipped_mgf(tmp_path: Path) -> Path:
    """The same MGF content, gzip-compressed."""
    path = tmp_path / "valid.mgf.gz"
    path.write_bytes(gzip.compress(_GOOD_MGF.encode("utf-8")))
    return path


@pytest.fixture()
def imzml_fixture(tmp_path: Path) -> Path:
    """A tiny 2-pixel imzML acquisition written with pyimzml.

    pyimzml ships with MassFlow (its imzML parser dependency), so the fixture
    is produced by the same ecosystem that reads it back; the test is skipped
    rather than faked when that dependency is absent.
    """
    pyimzml_writer = pytest.importorskip(
        "pyimzml.ImzMLWriter", reason="pyimzml (a MassFlow dependency) is absent"
    )
    base = tmp_path / "acquisition"
    writer = pyimzml_writer.ImzMLWriter(str(base), mode="continuous")
    writer.addSpectrum(
        np.array([100.0, 200.0, 300.0]),
        np.array([10.0, 20.0, 30.0], dtype=np.float32),
        (1, 1, 1),
    )
    writer.addSpectrum(
        np.array([100.0, 200.0, 300.0]),
        np.array([5.0, 15.0, 25.0], dtype=np.float32),
        (1, 2, 1),
    )
    writer.close()
    path = base.with_suffix(".imzML")
    assert path.is_file() and path.with_suffix(".ibd").is_file()
    return path
