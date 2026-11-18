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
from pathlib import Path
from typing import Any

import pytest

os.environ.setdefault("MSMCP_EMBEDDING_BACKEND", "mock")

from msmcp.server import mcp
from msmcp.tools.chem import AdductInput, IsotopeInput
from msmcp.tools.chromatograms import ChromatogramInput
from msmcp.tools.io import (
    ListSpectraInput,
    LoadSpectrumInput,
    MzMLParseInput,
    ReferenceInput,
)
from msmcp.tools.qc import QCInput
from msmcp.tools.search import PollInput, QueryContext, SearchInput, StatusInput
from msmcp.tools.similarity import ComputeCosineInput, ValidatePrecursorInput

# Tool name -> the Pydantic model that validates its arguments.
MODEL_FOR_TOOL: dict[str, type[Any]] = {
    "extract_chromatograms": ChromatogramInput,
    "load_mzml_summary": MzMLParseInput,
    "list_spectra": ListSpectraInput,
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


async def test_instructions_match_the_library_search_contract() -> None:
    """The server-wide orientation must not contradict the search tool.

    A host reads ``instructions`` before any tool description, so a stale
    sentence here shapes the model's plan more than a tool doc does.  This pins
    the contract ``search_library`` already advertises — MSP/NIST text is read
    for real, a path with no reader is refused, and the synthetic library is a
    demonstration reachable only by opting in — and fails if the two drift
    apart again.  (An earlier build told the model the search was *always*
    synthetic, which was false once the MSP reader landed; a later one told it
    an unreadable path silently falls back, which is false now.)
    """
    text = (mcp.instructions or "").lower()
    assert "msp" in text
    assert "for real" in text
    assert "synthetic" in text
    assert "refused" in text
    assert "msmcp_library_fallback=synthetic" in text
    # The retired wording, kept as an explicit tripwire.
    assert "still scans a synthetic in-memory library" not in text
    assert "only the query spectrum can be real" not in text
    assert "falls back" not in text


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


async def test_chromatogram_tool_publishes_a_structured_output_schema() -> None:
    tool = next(t for t in await _tools() if t.name == "extract_chromatograms")
    schema = tool.output_schema
    assert schema is not None
    properties = schema["properties"]
    assert set(properties) == {
        "schema_version",
        "source",
        "acquisition",
        "chromatograms",
        "extraction_parameters",
        "completeness",
        "missing_observations",
        "warnings",
        "provenance",
    }
    definitions = schema["$defs"]
    chromatograms = definitions["ChromatogramData"]["properties"]
    assert "retention_times_min" in chromatograms
    assert "polarities" in chromatograms
    assert "tic_intensities" in chromatograms
    assert "bpc_intensities" in chromatograms
    assert "xic" in chromatograms
    assert set(tool.input_schema.get("required", [])) == {"file_path"}
    targets_mz = tool.input_schema["properties"]["targets_mz"]
    while "items" not in targets_mz and "anyOf" in targets_mz:
        targets_mz = next(
            item for item in targets_mz["anyOf"] if item.get("type") == "array"
        )
    assert targets_mz["items"]["exclusiveMinimum"] == 0.0


async def test_chromatogram_tool_advertises_ten_thousand_scan_bound() -> None:
    tool = next(t for t in await _tools() if t.name == "extract_chromatograms")
    assert tool.input_schema["properties"]["max_spectra"]["maximum"] == 10_000
    assert (
        ChromatogramInput(file_path="sample.mzML", max_spectra=10_000).max_spectra
        == 10_000
    )


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
    # ...an unreadable path is refused rather than answered from a fabrication...
    assert "refused" in description
    # ...and the demonstration library is reachable only by opting in.
    assert "msmcp_library_fallback=synthetic" in description
    assert "never treat" in description
    # The parameter description is what a host reads while filling in the
    # argument, so it must not still promise the retired silent substitution.
    database_file = tool.input_schema["properties"]["database_file"]
    parameter = database_file["description"].lower()
    assert "falls back" not in parameter
    assert "refused" in parameter
    assert "msmcp_library_fallback=synthetic" in parameter


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
    library = "libraries/metabolomics.txt"
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


def test_report_pins_the_no_hit_line_a_host_receives() -> None:
    """The no-hit wording is report contract, so the fast suite pins it.

    A payload whose candidates are all excluded by the acquisition-context
    gate carries a single results line.  The evaluation notebook reads
    whichever results block the payload has, so a rename here would otherwise
    only surface in the slow ``make eval`` lane -- and only on a checkout whose
    placeholder library produced no candidates.
    """
    from msmcp.tools.search import QueryContext, SearchRequest, _run_scan

    # A declared MS2 precursor far outside the compound pool: every candidate
    # is excluded by the gate rather than the gate being skipped.
    report = _run_scan(
        SearchRequest(
            database_file="libraries/metabolomics.txt",
            experimental_peaks=((5000.0, 1.0), (138.0662, 0.5), (110.0713, 0.2)),
            experimental_file="experimental/run42.mzML",
            query_context=QueryContext(ms_level=2, precursor_mz=5000.0),
        )
    ).report

    no_hit_line = "**No context-eligible or context-unknown library candidates.**"
    assert no_hit_line in report
    assert "| Rank | Compound" not in report
    # The refusal is explained, not silent, and still sits under the banner.
    assert "excluded before ranking" in report
    assert report.index("SYNTHETIC LIBRARY") < report.index(no_hit_line)


@pytest.mark.parametrize("method", ["classical", "dreams", "lsm-ms2"])
def test_report_builder_is_well_formed_for_every_scorer(method: str) -> None:
    from msmcp.tools.search import SearchRequest, _run_scan

    report = _run_scan(
        SearchRequest(
            database_file="lib.mzML",
            experimental_peaks=((120.08, 100.0), (500.1, 55.0)),
            experimental_file="exp.mzML",
            scoring_method=method,  # type: ignore[arg-type]
        )
    ).report
    assert report.startswith(">")
    assert "SYNTHETIC LIBRARY" in report


def _write_library(
    path: Path,
    *,
    precursor_header: str | None = "PrecursorMZ",
    precursor_mz: float = 195.0877,
    name: str = "Caffeine",
) -> Path:
    """Write a one-record MSP library, optionally without a precursor header."""
    peaks = [(110.0713, 40.0), (120.0808, 100.0), (138.0662, 420.0)]
    header = [] if precursor_header is None else [f"{precursor_header}: {precursor_mz}"]
    lines = [
        f"Name: {name}",
        "Formula: C8H10N4O2",
        *header,
        "Ion_mode: P",
        f"Num Peaks: {len(peaks)}",
        *(f"{mz:.4f} {intensity:.1f}" for mz, intensity in peaks),
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


QUERY_PEAKS = ((110.0713, 40.0), (120.0808, 100.0), (138.0662, 420.0))


def _scan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    library: Path,
    context: QueryContext | None,
) -> str:
    from msmcp import security
    from msmcp.tools.search import SearchRequest, _run_scan

    policy = security.SecurityPolicy(allowed_root=tmp_path)
    monkeypatch.setattr(security, "DEFAULT_POLICY", policy)
    return _run_scan(
        SearchRequest(
            database_file=str(library),
            experimental_peaks=QUERY_PEAKS,
            experimental_file="experimental/run42.mgf",
            query_context=context,
        )
    ).report


def test_a_generated_library_keeps_its_precursor_through_reader_and_search(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A library written with `PrecursorMZ` is context-checked, not context-unknown.

    This is the failure the test exists for: writing the header as `PEPMASS`
    instead parses as an unknown field, every reference loses its precursor, and
    the search still runs while silently ceasing to be context-aware.
    """
    library = _write_library(tmp_path / "library.msp")
    report = _scan(
        tmp_path,
        monkeypatch,
        library,
        QueryContext(ms_level=2, precursor_mz=195.0877, source="file"),
    )

    assert "Context gate: precursor m/z within" in report
    assert "Context eligibility: 1 matched, 0 unknown, 0 excluded" in report


def test_a_query_without_a_precursor_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No usable query precursor: the gate is not applied, and the query is named."""
    library = _write_library(tmp_path / "library.msp")
    report = _scan(tmp_path, monkeypatch, library, None)

    assert "Context gate: not applied" in report
    assert "the query does not state a usable MS2 precursor" in report
    assert "not evaluable" not in report
    assert "Context eligibility: 0 matched, 1 unknown, 0 excluded" in report


def test_missing_reference_precursors_are_not_blamed_on_the_query(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A library stating no precursors makes the gate unevaluable, not skipped.

    The query here does state a precursor, so a report saying the query lacks
    one points a reader at the wrong side of the comparison -- and invites them
    to trust a context check that never ran.
    """
    library = _write_library(tmp_path / "library.msp", precursor_header=None)
    report = _scan(
        tmp_path,
        monkeypatch,
        library,
        QueryContext(ms_level=2, precursor_mz=195.0877, source="file"),
    )

    assert "Context gate: not evaluable" in report
    assert "0 of 1 library spectra state one" in report
    assert "the query does not state a usable MS2 precursor" not in report
    assert "Context eligibility: 0 matched, 1 unknown, 0 excluded" in report


def test_an_unrecognised_precursor_header_cannot_look_like_a_context_match(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The trap, end to end: a `PEPMASS` library still searches, nothing is checked.

    The search succeeds and returns hits, so the only signal that precursor
    compatibility was never evaluated is the report text. It must say the gate
    was unevaluable and must not report a single context-matched candidate.
    """
    library = _write_library(tmp_path / "library.msp", precursor_header="PEPMASS")
    report = _scan(
        tmp_path,
        monkeypatch,
        library,
        QueryContext(ms_level=2, precursor_mz=195.0877, source="file"),
    )

    assert "Context eligibility: 0 matched, 1 unknown, 0 excluded" in report
    assert "Context gate: not evaluable" in report
    assert "| Rank | Compound" in report  # hits are still returned, by design
