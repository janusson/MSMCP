"""Drive an installed MSMCP through one acquisition-to-interpretation workflow.

The client speaks MCP over stdio to the *installed* server — the one on PATH —
so what runs here is what a host LLM would run, not the checkout's source tree.

Sequence: quality control -> spectrum reference -> context-gated library search
-> poll for the report. The point of the exercise is the last step: the report
is written for a reader who has to decide what the numbers license.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

REQUIRED_TOOLS = {
    "generate_qc_summary",
    "load_spectrum",
    "search_library",
    "check_search_status",
}
POLL_TIMEOUT_S = 300.0
POLL_INTERVAL_S = 0.25


def _text(result: Any) -> str:
    """Flatten an MCP tool result into the text a host would show."""
    blocks = getattr(result, "content", None) or []
    return "\n".join(block.text for block in blocks if getattr(block, "text", None))


async def run(server_command: str, data_dir: Path, report_path: Path) -> dict[str, Any]:
    query = (data_dir / "query.mgf").resolve()
    library = (data_dir / "library.msp").resolve()
    for path in (query, library):
        if not path.is_file():
            raise SystemExit(f"missing {path}; run prepare_data.py first")

    environment = dict(os.environ)
    # The server may read only inside its boundary, so the boundary is the data
    # directory itself, wherever the caller prepared it.
    environment["MSMCP_ALLOWED_ROOT"] = str(data_dir.resolve())

    parameters = StdioServerParameters(command=server_command, args=[], env=environment)
    evidence: dict[str, Any] = {}

    async with (
        stdio_client(parameters) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()

        tools = {tool.name for tool in (await session.list_tools()).tools}
        missing = REQUIRED_TOOLS - tools
        if missing:
            raise SystemExit(f"the server does not advertise {sorted(missing)}")

        qc = await session.call_tool("generate_qc_summary", {"file_path": str(query)})
        evidence["qc"] = _text(qc)

        loaded = await session.call_tool(
            "load_spectrum", {"file_path": str(query), "spectrum_index": 0}
        )
        evidence["load"] = json.loads(_text(loaded))

        dispatched = await session.call_tool(
            "search_library",
            {
                "experimental_file": str(query),
                "database_file": str(library),
                "scoring_method": "classical",
            },
        )
        evidence["dispatch"] = _text(dispatched)
        job_id = _job_id(evidence["dispatch"])

        started = time.monotonic()
        status = await session.call_tool("check_search_status", {"job_id": job_id})
        while "Spectral Library Search Results" not in _text(status):
            if time.monotonic() - started > POLL_TIMEOUT_S:
                raise SystemExit(f"search {job_id} did not finish in {POLL_TIMEOUT_S}s")
            await asyncio.sleep(POLL_INTERVAL_S)
            status = await session.call_tool("check_search_status", {"job_id": job_id})

        evidence["report"] = _text(status)
        evidence["job"] = {
            "job_id": job_id,
            "wall_clock_s": round(time.monotonic() - started, 3),
        }

    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
    return evidence


def _job_id(dispatched: str) -> str:
    for line in dispatched.splitlines():
        if "Job ID" in line and "`" in line:
            return line.split("`")[1]
    raise SystemExit(f"no job id in the dispatch reply:\n{dispatched}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", default="msmcp", help="installed msmcp entry point")
    parser.add_argument(
        "--data",
        type=Path,
        default=Path(__file__).parent / "data",
        help="directory holding query.mgf and library.msp (also the server's root)",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path(__file__).parent / "workflow_report.json",
        help="file for the raw payloads (created with its parent directories)",
    )
    arguments = parser.parse_args()

    evidence = asyncio.run(run(arguments.server, arguments.data, arguments.out))

    report = evidence["report"]
    print("=" * 72)
    print("QUALITY CONTROL")
    print("=" * 72)
    print(evidence["qc"].strip()[:1200])
    print()
    print("=" * 72)
    print("SEARCH REPORT")
    print("=" * 72)
    print(report.strip())
    print()
    print(f"full evidence: {arguments.out}")
    print("read INTERPRETATION.md before drawing a conclusion from the ranking")


if __name__ == "__main__":
    main()
