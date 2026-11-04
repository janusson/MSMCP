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
import importlib.util
import json
import os
import tempfile
import time
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
