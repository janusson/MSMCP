"""Qualify an *installed* MSMCP with a real MCP stdio client.

This is an evidence script, not part of the unit gate.  It is run against a
fresh virtual environment that contains only the built wheel and its resolved
dependencies, so it exercises the installed entry point and the wire protocol
rather than the source tree.

It verifies:

* initialization and tool discovery over stdio;
* a genuine ingest -> QC -> context-gated library-search -> poll workflow;
* report/provenance disclosure and candidate eligibility;
* structured tool errors for an out-of-root path, a malformed library and an
  MS1 query;
* the library-fallback contract, each mode in its own server process: a library
  in an unsupported format is refused by default with a readable reason and no
  job; only the exact ``MSMCP_LIBRARY_FALLBACK=synthetic`` produces the labelled
  demonstration result, and misspelt opt-ins are refused; a real MSP library is
  read from disk and identified by its digest in every mode;
* the ``MSMCP_MAX_SPECTRA`` bound on an indexed read;
* that the session survives, which is only possible if nothing contaminates the
  server's stdout.

Run with the target environment, for example::

    /tmp/msmcp-qualify/bin/python validation/installed_mcp_qualification.py \
        --server-command /tmp/msmcp-qualify/bin/msmcp
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.util
import json
import os
import re
import tempfile
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

EXPECTED_TOOLS = {
    "ping",
    "load_mzml_summary",
    "list_spectra",
    "load_spectrum",
    "extract_chromatograms",
    "summarise_reference",
    "release_reference",
    "predict_adduct_offset",
    "annotate_isotopes",
    "validate_precursor",
    "compute_cosine",
    "generate_qc_summary",
    "search_library",
    "check_search_status",
    "cancel_search",
}

LIBRARY_MSP = """\
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

Name: Wrong precursor
Formula: C8H10N4O2
PrecursorMZ: 900.0000
Ion_mode: P
Num Peaks: 5
110.0713 40.0
120.0808 100.0
136.0757 60.0
138.0662 420.0
500.1 55.0

Name: Wrong polarity
Formula: C8H10N4O2
PrecursorMZ: 195.0877
Ion_mode: N
Num Peaks: 5
110.0713 40.0
120.0808 100.0
136.0757 60.0
138.0662 420.0
500.1 55.0
"""

QUERY_MGF = """\
BEGIN IONS
TITLE=Caffeine MS2 query
PEPMASS=195.0877
MSLEVEL=2
IONMODE=POSITIVE
CHARGE=1+
110.0713 40.0
120.0808 100.0
136.0757 60.0
138.0662 420.0
500.1 55.0
END IONS
"""

MS1_MGF = """\
BEGIN IONS
TITLE=MS1 survey scan
PEPMASS=195.0877
MSLEVEL=1
IONMODE=POSITIVE
110.0713 40.0
120.0808 100.0
END IONS
"""

# A real library in a format MSMCP has no reader for: the case the default
# refusal exists for.  It is never opened, so only its suffix matters, but it is
# written to disk so the refusal cannot be mistaken for a missing-file error.
LIBRARY_JSON = """\
[{"name": "Caffeine", "precursor_mz": 195.0877,
  "peaks": [[110.0713, 40.0], [120.0808, 100.0], [138.0662, 420.0]]}]
"""

# An MS2 query that states no precursor.  It is ranked on fragments alone, so
# against the demonstration library it yields a hit table rather than an empty
# precursor gate — the artefact the synthetic label has to accompany.
QUERY_WITHOUT_PRECURSOR_MGF = """\
BEGIN IONS
TITLE=MS2 query without a stated precursor
MSLEVEL=2
110.0713 40.0
120.0808 100.0
136.0757 60.0
138.0662 420.0
500.1 55.0
END IONS
"""

ENV_LIBRARY_FALLBACK = "MSMCP_LIBRARY_FALLBACK"
SYNTHETIC_OPT_IN = "synthetic"
# Values a person could type while meaning the opt-in.  None is the opt-in.
MISSPELT_OPT_INS = (
    "synth",
    "Synthetic",
    "SYNTHETIC",
    "synthetic ",
    " synthetic",
    "true",
    "1",
)

# The server logs this line, on stderr, each time it hands a search to the job
# executor.  Counting it is how "no job was created" is observed from outside.
_DISPATCH_LOG_MARKER = "Dispatched search job"
_JOB_ID = re.compile(r"\b[0-9a-f]{32}\b")
_HIT_TABLE_HEADER = "| Rank | Compound | Score |"


def _text(result: Any) -> str:
    """Return the concatenated text content of a tool result."""
    return "\n".join(
        item.text for item in result.content if getattr(item, "type", "") == "text"
    )


def _write_fixtures(root: Path) -> tuple[Path, Path, Path, Path]:
    library = root / "library.msp"
    library.write_text(LIBRARY_MSP, encoding="utf-8")
    query = root / "query.mgf"
    query.write_text(QUERY_MGF, encoding="utf-8")
    ms1 = root / "ms1.mgf"
    ms1.write_text(MS1_MGF, encoding="utf-8")
    malformed = root / "malformed.msp"
    malformed.write_text("Name: Broken\nNum Peaks: 5\n100.0 1.0\n", encoding="utf-8")
    return library, query, ms1, malformed


async def _call(session: ClientSession, name: str, arguments: dict[str, Any]) -> Any:
    return await session.call_tool(name, arguments)


async def _poll_report(
    session: ClientSession, job_id: str, timeout: float = 60.0
) -> str:
    deadline = time.monotonic() + timeout
    last = ""
    while time.monotonic() < deadline:
        result = await _call(
            session, "check_search_status", {"job_id": job_id, "full_report": True}
        )
        last = _text(result)
        if result.is_error:
            raise RuntimeError(f"poll failed: {last}")
        if "Spectral Library Search Results" in last:
            return last
        await asyncio.sleep(0.1)
    raise TimeoutError(f"search did not finish; last poll: {last!r}")


async def qualify(server_command: str) -> dict[str, Any]:
    evidence: dict[str, Any] = {}
    with tempfile.TemporaryDirectory(prefix="msmcp-installed-") as raw_root:
        root = Path(raw_root).resolve()
        library, query, ms1, malformed = _write_fixtures(root)
        outside = root.parent / "msmcp-outside-allowed-root.mgf"
        outside.write_text(QUERY_MGF, encoding="utf-8")

        env = dict(os.environ)
        env["MSMCP_ALLOWED_ROOT"] = str(root)
        env["MSMCP_MAX_SPECTRA"] = "2"
        env.pop("MSMCP_EMBEDDING_BACKEND", None)  # use the real default backend
        env.pop(ENV_LIBRARY_FALLBACK, None)  # and the default library fallback

        params = StdioServerParameters(
            command=server_command,
            args=[],
            env=env,
            cwd=str(root),
        )

        async with (
            stdio_client(params) as (read, write),
            ClientSession(read, write) as session,
        ):
            init = await session.initialize()
            evidence["server"] = {
                "name": init.server_info.name,
                "version": init.server_info.version,
                "protocol": init.protocol_version,
            }

            listed = await session.list_tools()
            names = {tool.name for tool in listed.tools}
            evidence["tools"] = sorted(names)
            missing = EXPECTED_TOOLS - names
            assert not missing, f"missing tools: {sorted(missing)}"

            ping = await _call(session, "ping", {})
            assert not ping.is_error, _text(ping)
            evidence["ping"] = _text(ping)

            formula = await _call(
                session,
                "annotate_isotopes",
                {"identifier": "C6H12O6", "is_smiles": False},
            )
            formula_text = _text(formula)
            assert not formula.is_error, formula_text
            assert "## Isotope Pattern: C6H12O6" in formula_text
            assert "180.0634 Da" in formula_text
            evidence["formula_chemistry"] = formula_text

            malformed_formula = await _call(
                session,
                "annotate_isotopes",
                {"identifier": "C6H12O6!", "is_smiles": False},
            )
            malformed_formula_text = _text(malformed_formula)
            assert not malformed_formula.is_error, malformed_formula_text
            assert malformed_formula_text.startswith("ERROR:")
            assert "Isotope Pattern" not in malformed_formula_text
            evidence["malformed_formula"] = malformed_formula_text

            smiles = await _call(
                session,
                "annotate_isotopes",
                {"identifier": "CO", "is_smiles": True},
            )
            smiles_text = _text(smiles)
            assert not smiles.is_error, smiles_text
            if importlib.util.find_spec("rdkit") is None:
                assert smiles_text.startswith("ERROR:")
                assert "Isotope Pattern" not in smiles_text
                evidence["smiles_mode"] = "core-only: failed closed without RDKit"
            else:
                assert "## Isotope Pattern: CH4O" in smiles_text
                evidence["smiles_mode"] = "RDKit-enabled: CO resolved as CH4O"
            evidence["smiles_chemistry"] = smiles_text

            adduct_expectations = {
                "[M+2H]2+": "+2.014553 Da",
                "[M+3H]3+": "+3.021829 Da",
                "[M+2Na-H]+": "+44.971165 Da",
                "[M+Na-2H]-": "+20.974668 Da",
            }
            adduct_evidence: dict[str, str] = {}
            for adduct, expected_shift in adduct_expectations.items():
                result = await _call(
                    session,
                    "predict_adduct_offset",
                    {"adduct_string": adduct},
                )
                result_text = _text(result)
                assert not result.is_error, result_text
                assert f"Exact mass shift (Δ): {expected_shift}" in result_text
                charge_denominators = {
                    "[M+2H]2+": "|2|",
                    "[M+3H]3+": "|3|",
                    "[M+Na-2H]-": "|-1|",
                }
                if adduct in charge_denominators:
                    assert charge_denominators[adduct] in result_text
                adduct_evidence[adduct] = result_text
            evidence["adduct_chemistry"] = adduct_evidence

            qc = await _call(session, "generate_qc_summary", {"file_path": str(query)})
            assert not qc.is_error, _text(qc)
            evidence["qc"] = _text(qc)

            loaded = await _call(
                session,
                "load_spectrum",
                {"file_path": str(query), "spectrum_index": 0},
            )
            assert not loaded.is_error, _text(loaded)
            structured = loaded.structured_content or {}
            evidence["load"] = structured
            reference = str(structured["reference"])
            assert reference.startswith("ptr:spectrum:")

            dispatched = await _call(
                session,
                "search_library",
                {
                    "experimental_file": str(query),
                    "spectrum_index": 0,
                    "database_file": str(library),
                    "scoring_method": "classical",
                    "instrument_class": "generic",
                },
            )
            assert not dispatched.is_error, _text(dispatched)
            dispatch_text = _text(dispatched)
            job_id = dispatch_text.split("`")[1]
            evidence["dispatch"] = dispatch_text

            report = await _poll_report(session, job_id)
            evidence["report"] = report
            assert "REAL LIBRARY" in report
            assert "| 1 | Caffeine | 1.0000 |" in report
            assert "Wrong precursor" not in report
            assert "Wrong polarity" not in report
            assert "precursor outside declared tolerance" in report
            assert "polarity mismatch" in report
            # The library record does not state a charge, so it stays
            # ranked but is labelled unknown rather than promoted.
            assert "unknown: reference charge not stated" in report
            assert "Provenance: search_library" in report
            assert "SIGNIFICANCE UNCALIBRATED" in report
            assert "| p-value" not in report.lower()

            summarised = await _call(
                session, "summarise_reference", {"reference": reference}
            )
            assert not summarised.is_error, _text(summarised)
            evidence["summarise"] = _text(summarised)

            released = await _call(
                session, "release_reference", {"reference": reference}
            )
            assert not released.is_error, _text(released)

            outside_result = await _call(
                session, "load_spectrum", {"file_path": str(outside)}
            )
            assert outside_result.is_error, _text(outside_result)
            evidence["outside_root_error"] = _text(outside_result)

            malformed_result = await _call(
                session,
                "search_library",
                {"experimental_file": str(query), "database_file": str(malformed)},
            )
            assert malformed_result.is_error, _text(malformed_result)
            evidence["malformed_library_error"] = _text(malformed_result)

            ms1_result = await _call(
                session,
                "search_library",
                {"experimental_file": str(ms1), "database_file": str(library)},
            )
            assert ms1_result.is_error, _text(ms1_result)
            # The transport reports is_error; the SDK's message is generic.
            evidence["ms1_error"] = _text(ms1_result)

            # MSMCP_MAX_SPECTRA=2: index 5 requires reading six spectra.
            bounded = await _call(
                session,
                "load_spectrum",
                {"file_path": str(query), "spectrum_index": 5},
            )
            assert bounded.is_error, _text(bounded)
            evidence["spectrum_bound_error"] = _text(bounded)

            # A second call proves the session is still healthy after the
            # structured errors; nothing has corrupted the stdio stream.
            final_ping = await _call(session, "ping", {})
            assert not final_ping.is_error, _text(final_ping)
            evidence["session_survived_errors"] = True

        outside.unlink(missing_ok=True)

    evidence["library_fallback"] = await qualify_library_fallback(server_command)
    return evidence


@asynccontextmanager
async def _fallback_session(
    server_command: str,
    root: Path,
    library_fallback: str | None,
    server_log: Path,
) -> AsyncIterator[ClientSession]:
    """Run one server with ``MSMCP_LIBRARY_FALLBACK`` set to *library_fallback*.

    ``None`` leaves the variable unset.  The flag is read from the server's
    environment, so each mode needs its own process.  The server's stderr goes
    to *server_log* so the caller can count dispatched jobs.
    """
    env = dict(os.environ)
    env["MSMCP_ALLOWED_ROOT"] = str(root)
    env.pop("MSMCP_EMBEDDING_BACKEND", None)
    env.pop(ENV_LIBRARY_FALLBACK, None)
    if library_fallback is not None:
        env[ENV_LIBRARY_FALLBACK] = library_fallback
    params = StdioServerParameters(
        command=server_command, args=[], env=env, cwd=str(root)
    )
    with server_log.open("w", encoding="utf-8") as errlog:
        async with (
            stdio_client(params, errlog=errlog) as (read, write),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            yield session


def _jobs_dispatched(server_log: Path) -> int:
    """How many searches this server has handed to its job executor so far."""
    log = server_log.read_text(encoding="utf-8", errors="replace")
    return log.count(_DISPATCH_LOG_MARKER)


async def _search(session: ClientSession, query: Path, database: Path) -> Any:
    return await _call(
        session,
        "search_library",
        {
            "experimental_file": str(query),
            "spectrum_index": 0,
            "database_file": str(database),
            "scoring_method": "classical",
            "instrument_class": "generic",
        },
    )


async def _assert_refused(
    session: ClientSession, query: Path, unsupported: Path, server_log: Path
) -> str:
    """An unsupported library is a readable tool error with no job behind it."""
    before = _jobs_dispatched(server_log)
    result = await _search(session, query, unsupported)
    text = _text(result)
    assert result.is_error, text
    # Actionable: what was wrong, what is readable, and how to opt in.
    assert unsupported.name in text
    assert "not a spectral library MSMCP can read" in text
    assert "the search was not run" in text
    for suffix in (".msp", ".mgf", ".sqlite"):
        assert suffix in text, f"refusal does not name {suffix}: {text}"
    assert f"{ENV_LIBRARY_FALLBACK}={SYNTHETIC_OPT_IN}" in text
    # The reason above is the point: an undeliberate failure reaches the client
    # as the bare "Error executing tool search_library", with none of it.
    # No job, and nothing that could be read as a result.
    assert "Job ID" not in text
    assert _JOB_ID.search(text) is None, text
    assert "Spectral Library Search Results" not in text
    assert _HIT_TABLE_HEADER not in text
    assert _jobs_dispatched(server_log) == before
    return text


async def _assert_real_library(
    session: ClientSession, query: Path, library: Path, server_log: Path
) -> str:
    """A valid MSP library is read from disk and identified by its digest."""
    before = _jobs_dispatched(server_log)
    dispatched = await _search(session, query, library)
    dispatch_text = _text(dispatched)
    assert not dispatched.is_error, dispatch_text
    assert "will be read from disk (MSP)" in dispatch_text
    # The positive control for the refusal check: a real dispatch is logged.
    assert _jobs_dispatched(server_log) == before + 1
    report = await _poll_report(session, dispatch_text.split("`")[1])
    digest = "sha256:" + hashlib.sha256(library.read_bytes()).hexdigest()
    assert "REAL LIBRARY — READ FROM DISK" in report
    assert f"Library digest: `{digest}`" in report
    assert f"`{library}` — MSP library read from disk (3 spectra)" in report
    assert "Library: MSP read from disk (3 spectra)" in report
    assert "| 1 | Caffeine | 1.0000 |" in report
    assert "SYNTHETIC" not in report
    assert "synthetic" not in report
    return report


async def qualify_library_fallback(server_command: str) -> dict[str, Any]:
    """Qualify what a library MSMCP cannot read does, in every fallback mode."""
    evidence: dict[str, Any] = {}
    with tempfile.TemporaryDirectory(prefix="msmcp-fallback-") as raw_base:
        base = Path(raw_base).resolve()
        root = base / "data"
        logs = base / "logs"
        root.mkdir()
        logs.mkdir()
        library, query, _, _ = _write_fixtures(root)
        unsupported = root / "library.json"
        unsupported.write_text(LIBRARY_JSON, encoding="utf-8")
        unanchored = root / "query-without-precursor.mgf"
        unanchored.write_text(QUERY_WITHOUT_PRECURSOR_MGF, encoding="utf-8")
        evidence["fixtures"] = {
            "unsupported_library": unsupported.name,
            "real_library": library.name,
            "real_library_sha256": hashlib.sha256(library.read_bytes()).hexdigest(),
            "query": query.name,
        }

        # 1. Default settings: refused, with a reason, and no job.  The same
        #    server then searches a real MSP library, so the refusal is about
        #    the format and not about search being unavailable.
        log = logs / "default.log"
        async with _fallback_session(server_command, root, None, log) as session:
            refusal = await _assert_refused(session, query, unsupported, log)
            assert "not to that exact value" not in refusal
            report = await _assert_real_library(session, query, library, log)
            evidence["default"] = {
                "flag": None,
                "refusal": refusal,
                "jobs_dispatched_by_refusal": 0,
                "real_library_report": report,
            }

        # The documented spelling of the default behaves identically.
        log = logs / "refuse.log"
        async with _fallback_session(server_command, root, "refuse", log) as session:
            refusal = await _assert_refused(session, query, unsupported, log)
            assert "not to that exact value" not in refusal
            evidence["explicit_refuse"] = {"flag": "refuse", "refusal": refusal}

        # 2. The exact opt-in: a job runs, and every reply says it is a
        #    demonstration — the dispatch, the report and the repeat-poll digest.
        log = logs / "synthetic.log"
        async with _fallback_session(
            server_command, root, SYNTHETIC_OPT_IN, log
        ) as session:
            dispatched = await _search(session, query, unsupported)
            dispatch_text = _text(dispatched)
            assert not dispatched.is_error, dispatch_text
            assert "a **synthetic** library is substituted" in dispatch_text
            assert "do not treat its hits as compound identifications" in dispatch_text
            assert _jobs_dispatched(log) == 1
            job_id = dispatch_text.split("`")[1]
            report = await _poll_report(session, job_id)
            assert "SYNTHETIC LIBRARY — NOT A COMPOUND IDENTIFICATION" in report
            assert f"`{unsupported}` — not opened" in report
            assert "substituted a synthetic library of" in report
            assert "Library: synthetic (" in report
            assert f"not read from `{unsupported}`" in report
            assert "REAL LIBRARY" not in report
            assert "Library digest" not in report
            # None of the unopened file's content can have reached the report.
            assert "Caffeine" not in report
            digest_poll = await _call(
                session, "check_search_status", {"job_id": job_id}
            )
            digest_text = _text(digest_poll)
            assert not digest_poll.is_error, digest_text
            assert "synthetic spectra (not read from disk)" in digest_text
            assert "**Synthetic library** — not compound identifications" in digest_text
            # The query above states a precursor, so the gate leaves nothing to
            # rank.  One that states none is ranked on fragments, and the hit
            # table it produces still sits under the banner.
            ranked = await _search(session, unanchored, unsupported)
            ranked_text = _text(ranked)
            assert not ranked.is_error, ranked_text
            ranked_report = await _poll_report(session, ranked_text.split("`")[1])
            banner = ranked_report.index("SYNTHETIC LIBRARY — NOT A COMPOUND")
            assert banner < ranked_report.index(_HIT_TABLE_HEADER)
            assert "Library: synthetic (" in ranked_report
            assert "REAL LIBRARY" not in ranked_report
            # Opting in changes nothing for a library MSMCP can read.
            real_report = await _assert_real_library(session, query, library, log)
            evidence["synthetic_opt_in"] = {
                "flag": SYNTHETIC_OPT_IN,
                "dispatch": dispatch_text,
                "report": report,
                "repeat_poll_digest": digest_text,
                "ranked_report": ranked_report,
                "real_library_report": real_report,
            }

        #    A near-miss is not the opt-in: refused, no job, and told why.
        misspelt: dict[str, str] = {}
        for index, value in enumerate(MISSPELT_OPT_INS):
            log = logs / f"misspelt-{index}.log"
            async with _fallback_session(server_command, root, value, log) as session:
                refusal = await _assert_refused(session, query, unsupported, log)
                assert "not to that exact value" in refusal, refusal
                assert _jobs_dispatched(log) == 0
                misspelt[value] = refusal
        evidence["misspelt_opt_ins"] = misspelt

    return evidence


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--server-command",
        default="msmcp",
        help="Installed msmcp entry point (default: msmcp on PATH).",
    )
    arguments = parser.parse_args()
    evidence = asyncio.run(qualify(arguments.server_command))
    print(json.dumps(evidence, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
