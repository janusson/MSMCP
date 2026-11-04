"""Run one bounded, real-library search through an installed MSMCP MCP server.

The query is a fixed synthetic performance fixture. The result measures parsing,
scanning, response size and child-process peak RSS; it is not identification or
accuracy evidence. Keep the server command pointed at a separately installed
wheel or sdist environment.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import resource
import tempfile
import time
from pathlib import Path
from typing import Any

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

QUERY = """BEGIN IONS
TITLE=qualification performance-only query
PEPMASS=195.0877
MSLEVEL=2
IONMODE=POSITIVE
CHARGE=1+
110.0713 40
120.0808 100
136.0757 60
138.0662 420
500.1 55
END IONS
"""


class QualificationError(RuntimeError):
    """The installed server did not complete the bounded performance request."""


def _text(response: Any) -> str:
    return "\n".join(
        item.text for item in response.content if getattr(item, "type", "") == "text"
    )


async def run_probe(
    server_command: str, library_path: Path, expected_sha256: str
) -> dict[str, Any]:
    library = library_path.resolve(strict=True)
    root = library.parent
    with library.open("rb") as handle:
        digest = hashlib.file_digest(handle, "sha256").hexdigest()
    if digest != expected_sha256.lower():
        raise QualificationError(
            f"library SHA-256 mismatch: expected {expected_sha256}, observed {digest}"
        )

    environment = dict(os.environ)
    environment["MSMCP_ALLOWED_ROOT"] = str(root)
    environment.pop("PYTHONPATH", None)
    environment.pop("VIRTUAL_ENV", None)
    server = StdioServerParameters(
        command=server_command,
        args=[],
        env=environment,
        cwd=str(root),
    )
    start = time.perf_counter()
    report = ""
    server_version = "unknown"

    with tempfile.TemporaryDirectory(prefix="msmcp-performance-", dir=root) as raw:
        query = Path(raw) / "query.mgf"
        query.write_text(QUERY, encoding="utf-8")
        async with (
            stdio_client(server) as (read, write),
            ClientSession(read, write) as session,
        ):
            initialized = await session.initialize()
            server_version = initialized.server_info.version
            dispatched = await session.call_tool(
                "search_library",
                {
                    "database_file": str(library),
                    "experimental_file": str(query),
                    "spectrum_index": 0,
                    "chunk_size": 2_000,
                    "scoring_method": "classical",
                    "instrument_class": "generic",
                },
            )
            if dispatched.is_error:
                raise QualificationError(_text(dispatched))
            job_match = re.search(r"[0-9a-f]{32}", _text(dispatched))
            if job_match is None:
                raise QualificationError("search dispatch returned no job ID")
            job_id = job_match.group(0)
            while time.perf_counter() - start < 240.0:
                status = await session.call_tool(
                    "check_search_status",
                    {"job_id": job_id, "full_report": True},
                )
                if status.is_error:
                    raise QualificationError(_text(status))
                report = _text(status)
                if "Spectral Library Search Results" in report:
                    break
                if "Failed" in report:
                    raise QualificationError(report)
                await asyncio.sleep(0.1)
            else:
                raise TimeoutError("large-library search exceeded 240 seconds")

    records_match = re.search(r"([0-9,]+) spectra", report)
    return {
        "status": "completed",
        "query": "fixed synthetic performance fixture; not identification evidence",
        "server_version": server_version,
        "library_path": str(library),
        "library_bytes": library.stat().st_size,
        "library_sha256": digest,
        "library_spectra_reported": (
            records_match.group(1) if records_match is not None else None
        ),
        "chunk_size": 2_000,
        "search_elapsed_seconds": round(time.perf_counter() - start, 3),
        "response_bytes": len(report.encode("utf-8")),
        "max_child_rss_bytes_macos": resource.getrusage(
            resource.RUSAGE_CHILDREN
        ).ru_maxrss,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server-command", required=True)
    parser.add_argument("--library", required=True, type=Path)
    parser.add_argument("--sha256", required=True)
    args = parser.parse_args()
    if re.fullmatch(r"[0-9a-fA-F]{64}", args.sha256) is None:
        parser.error("--sha256 must be a 64-character SHA-256 hex digest")
    result = asyncio.run(run_probe(args.server_command, args.library, args.sha256))
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
