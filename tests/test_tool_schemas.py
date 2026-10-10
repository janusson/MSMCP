"""Wire-contract tests for the MCP tool surface.

Everything here asserts on what a host LLM actually receives from
``tools/list`` and from a completed search report, not on how the tools are
implemented.  The MCP SDK builds each ``inputSchema`` from the tool
*signature*, so a parameter documented only on the validating Pydantic model
is invisible to the host; these tests fail if that happens again.

The constraint tests are deliberately duplicated knowledge: numeric bounds
live both on the model (enforced in the tool body) and on the signature
(advertised on the wire).  If the two drift apart, the assertions below are
what catches it.
"""

from __future__ import annotations

import os
from typing import Any

import pytest

os.environ.setdefault("MSMCP_EMBEDDING_BACKEND", "mock")

from msmcp.server import mcp
from msmcp.tools.chem import AdductInput, IsotopeInput
from msmcp.tools.io import LoadSpectrumInput, MzMLParseInput, ReferenceInput
from msmcp.tools.qc import QCInput
from msmcp.tools.search import PollInput, SearchInput, StatusInput
from msmcp.tools.similarity import ComputeCosineInput, ValidatePrecursorInput

# Tool name -> the Pydantic model that validates its arguments.
MODEL_FOR_TOOL: dict[str, type[Any]] = {
    "load_mzml_summary": MzMLParseInput,
    "load_spectrum": LoadSpectrumInput,
    "summarise_reference": ReferenceInput,
    "release_reference": ReferenceInput,
    "generate_qc_summary": QCInput,
    "predict_adduct_offset": AdductInput,
    "annotate_isotopes": IsotopeInput,
    "validate_precursor": ValidatePrecursorInput,
    "compute_cosine": ComputeCosineInput,
    "search_library": SearchInput,
    "check_search_status": PollInput,
    "cancel_search": StatusInput,
}

# Tools that deliberately mutate server-side state.  Everything else must
# advertise itself as read-only so a host can gate it.
WRITING_TOOLS = {"cancel_search", "release_reference"}

# Tools whose arguments are the empty set: there is no input model to validate
# against and no signature/model comparison to make.  Named rather than skipped,
# because both tests below walk the published surface with
# ``MODEL_FOR_TOOL.get(...)`` and an unnamed tool silently escapes them -- which
# is how ``ping`` went unchecked while it was registered outside the
# ``register_tools()`` convention every other tool module follows.
TOOLS_WITHOUT_INPUT_MODEL = {"ping"}

# Symbols that only make sense to a reader of the source.  A tool description
# is prose for a language model choosing a tool; leaking internals wastes
# context and invites the host to reason about implementation, not behaviour.
INTERNAL_SYMBOLS = (
    "_EXECUTOR",
    "_run_scan",
    "_build_mock_database",
    "LocalAsyncExecutor",
    "asyncio.",
    "EmbeddingBackendUnavailable",
    "MalformedFileError",
    "PathEscapeError",
)

_TOOLS: list[Any] | None = None


async def _tools() -> list[Any]:
    """The registered tools, fetched once per session."""
    global _TOOLS
    if _TOOLS is None:
        _TOOLS = await mcp.list_tools()
    return _TOOLS


# ---------------------------------------------------------------------------
# Display metadata
# ---------------------------------------------------------------------------
async def test_every_tool_has_a_title_and_description() -> None:
    for tool in await _tools():
        assert tool.title, f"{tool.name} has no title"
        assert tool.description, f"{tool.name} has no description"
        assert tool.description.strip() != tool.name


async def test_every_tool_declares_annotations() -> None:
    """Hosts gate side-effecting calls on these hints, so none may be None."""
    for tool in await _tools():
        assert tool.annotations is not None, f"{tool.name} has no annotations"
        assert tool.annotations.read_only_hint is not None, (
            f"{tool.name} does not state whether it is read-only"
        )
        assert tool.annotations.open_world_hint is not None, (
            f"{tool.name} does not state whether it reaches the network"
        )


async def test_only_the_writing_tools_are_not_read_only() -> None:
    """Exactly the state-mutating tools must advertise read-only=False."""
    writing = {
        tool.name
        for tool in await _tools()
        if tool.annotations is not None and tool.annotations.read_only_hint is False
    }
    assert writing == WRITING_TOOLS


async def test_no_tool_reaches_the_network() -> None:
    for tool in await _tools():
        assert tool.annotations is not None
        assert tool.annotations.open_world_hint is False, (
            f"{tool.name} is marked open-world but the server makes no network calls"
        )


async def test_server_publishes_instructions() -> None:
    assert mcp.instructions, "hosts get no orientation before choosing a tool"


async def test_tool_descriptions_do_not_leak_internals() -> None:
    for tool in await _tools():
        for symbol in INTERNAL_SYMBOLS:
            assert symbol not in (tool.description or ""), (
                f"{tool.name} description mentions internal symbol {symbol!r}"
            )


# ---------------------------------------------------------------------------
# Parameter documentation — the regression this module exists for
# ---------------------------------------------------------------------------
async def test_every_parameter_is_documented() -> None:
    """A host that cannot read a parameter cannot fill it correctly."""
    missing: list[str] = []
    for tool in await _tools():
        for name, spec in tool.input_schema.get("properties", {}).items():
            if not spec.get("description"):
                missing.append(f"{tool.name}.{name}")
    assert not missing, f"parameters with no description on the wire: {missing}"


async def test_signature_names_match_the_validating_model() -> None:
    """The wire arguments must line up with what the body validates."""
    for tool in await _tools():
        model = MODEL_FOR_TOOL.get(tool.name)
        if model is None:
            continue
        assert set(tool.input_schema.get("properties", {})) == set(model.model_fields)
        assert set(tool.input_schema.get("required", [])) == {
            name for name, f in model.model_fields.items() if f.is_required()
        }


async def test_every_published_tool_is_modelled_or_declared_unmodelled() -> None:
    """No tool may escape the signature/model comparison by being absent from it."""
    published = {tool.name for tool in await _tools()}
    modelled = set(MODEL_FOR_TOOL) | TOOLS_WITHOUT_INPUT_MODEL
    assert published == modelled, (
        f"covered by neither an input model nor the unmodelled set: "
        f"{sorted(published - modelled)}; declared but not published: "
        f"{sorted(modelled - published)}"
    )


async def test_ping_takes_no_arguments() -> None:
    """The one unmodelled tool is unmodelled because it has no parameters."""
    tool = next(t for t in await _tools() if t.name == "ping")
    assert tool.input_schema.get("properties", {}) == {}
    assert set(tool.input_schema.get("required", [])) == set()


async def test_compute_cosine_advertises_one_peak_source_per_side() -> None:
    """Both spellings of each side reach the wire, and the exclusion is stated.

    The mutual exclusion is enforced by the model validator; a host can only
    respect it if the schema names both parameters and says they are
    alternatives, and if neither is marked required.
    """
    tool = next(t for t in await _tools() if t.name == "compute_cosine")
    properties = tool.input_schema["properties"]
    for side in ("query", "reference"):
        peaks, reference = f"{side}_peaks", f"{side}_reference"
        assert peaks in properties, f"{peaks} is not on the wire"
        assert reference in properties, f"{reference} is not on the wire"
        assert reference in properties[peaks]["description"]
        assert peaks in properties[reference]["description"]
    assert not set(tool.input_schema.get("required", [])) & {
        "query_peaks",
        "query_reference",
        "reference_peaks",
        "reference_reference",
    }


# ---------------------------------------------------------------------------
# Constraint drift
# ---------------------------------------------------------------------------
_JSON_KEY_FOR_CONSTRAINT: dict[str, tuple[str, ...]] = {
    "ge": ("minimum",),
    "le": ("maximum",),
    "gt": ("exclusiveMinimum",),
    "lt": ("exclusiveMaximum",),
    "min_length": ("minLength", "minItems"),
    "max_length": ("maxLength", "maxItems"),
}


def _declared_constraints(field: Any) -> dict[str, Any]:
    """Extract the constraint metadata pydantic recorded on a model field."""
    found: dict[str, Any] = {}
    for constraint in _JSON_KEY_FOR_CONSTRAINT:
        value = next(
            (m for m in field.metadata if getattr(m, constraint, None) is not None),
            None,
        )
        if value is not None:
            found[constraint] = getattr(value, constraint)
    return found


def _wire_constraint(spec: dict[str, Any], keys: tuple[str, ...]) -> Any:
    """Look up a JSON-Schema keyword at the top level or inside a union.

    A parameter declared ``X | None`` has its type-specific keywords nested
    under ``anyOf`` (``[<the type>, {"type": "null"}]``), so a top-level-only
    lookup silently stops checking an optional parameter the moment it gains a
    default.  Searching the union branches keeps those bounds guarded.
    """
    for key in keys:
        if key in spec:
            return spec[key]
    for keyword in ("anyOf", "oneOf"):
        for branch in spec.get(keyword, []):
            if isinstance(branch, dict):
                nested = _wire_constraint(branch, keys)
                if nested is not None:
                    return nested
    return None


async def test_signature_advertises_the_models_constraints() -> None:
    """Bounds enforced in the body must also appear in the wire schema.

    Otherwise the schema invites an argument the tool then rejects, and the
    host has no way to know the valid range.
    """
    problems: list[str] = []
    for tool in await _tools():
        model = MODEL_FOR_TOOL.get(tool.name)
        if model is None:
            continue
        for name, field in model.model_fields.items():
            spec = tool.input_schema["properties"].get(name, {})
            for constraint, expected in _declared_constraints(field).items():
                keys = _JSON_KEY_FOR_CONSTRAINT[constraint]
                actual = _wire_constraint(spec, keys)
                if actual != expected:
                    problems.append(
                        f"{tool.name}.{name}: {constraint}={expected!r} not in "
                        f"schema (looked for {keys}, found {actual!r})"
                    )
    assert not problems, "constraints missing from the wire schema:\n" + "\n".join(
        problems
    )


# ---------------------------------------------------------------------------
# Truthfulness of the search pipeline
# ---------------------------------------------------------------------------
async def test_search_library_declares_when_its_library_is_real_or_synthetic() -> None:
    """The tool must not let a host mistake its output for an identification."""
    tool = next(t for t in await _tools() if t.name == "search_library")
    description = (tool.description or "").lower()
    # A supported library format is read from disk through the security boundary...
    assert "read for real" in description
    assert "msp" in description
    # ...and a path with no reader is a labelled synthetic fallback, not silence.
    assert "synthetic library" in description
    assert "never treat" in description


def test_search_report_carries_the_provenance_banner() -> None:
    """A synthetic-library hit table must say so, in the report itself."""
    from msmcp.tools.search import (
        SearchRequest,
        _build_mock_database,
        _library_spec,
        _run_scan,
    )

    # A real library spectrum as the query: a known true positive, so the
    # report contains an actual hit table to order the banner against.
    library = "libraries/metabolomics.db"
    n_spectra, seed = _library_spec(library)
    conn = _build_mock_database(n_spectra=n_spectra, seed=seed)
    try:
        spectrum_id = conn.execute(
            "SELECT spectrum_id FROM peaks GROUP BY spectrum_id "
            "HAVING COUNT(*) >= 8 ORDER BY spectrum_id LIMIT 1"
        ).fetchone()[0]
        query = tuple(
            conn.execute(
                "SELECT mz, intensity FROM peaks WHERE spectrum_id=? ORDER BY mz",
                (spectrum_id,),
            ).fetchall()
        )
    finally:
        conn.close()
    assert len(query) >= 8

    report = _run_scan(
        SearchRequest(
            database_file=library,
            experimental_peaks=query,
            experimental_file="experimental/run42.mzML",
        )
    ).report
    assert "SYNTHETIC LIBRARY" in report
    assert "NOT A COMPOUND IDENTIFICATION" in report
    # A spectrum that is *in* the library must be found, and the banner must
    # precede the hit table, so it survives truncation by a host that only
    # forwards the head of a tool result.
    assert "No hits passed the significance threshold" not in report
    assert report.index("SYNTHETIC LIBRARY") < report.index("| Rank | Compound")
    # The library path is echoed for traceability but must not be presented as
    # having been read, while the query genuinely was.
    assert "not opened" in report
    assert "read from disk" in report


@pytest.mark.parametrize("method", ["classical", "dreams", "lsm-ms2"])
def test_report_builder_is_well_formed_for_every_scorer(method: str) -> None:
    from msmcp.tools.search import SearchRequest, _run_scan

    report = _run_scan(
        SearchRequest(
            database_file="lib.db",
            experimental_peaks=((120.08, 100.0), (500.1, 55.0)),
            experimental_file="exp.mzML",
            scoring_method=method,  # type: ignore[arg-type]
        )
    ).report
    assert report.startswith(">")
    assert "SYNTHETIC LIBRARY" in report
