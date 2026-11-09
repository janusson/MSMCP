"""Smoke test for the stdio server a host actually launches.

Every other test in this suite imports the tool functions directly, so a break
in the entry point that ``pip install msmcp`` provides -- the console-script
wiring, the process lifecycle, the JSON-RPC framing -- would only surface after
release.  This spawns the real server and speaks the protocol to it.

The harness keeps stdin open while it waits for answers, the way a host does,
and bounds every wait: a server that hangs fails the test instead of hanging
the suite.
"""

from __future__ import annotations

import json
import queue
import subprocess
import sys
import threading
from importlib.metadata import version
from typing import Any, Final

import pytest

DOCUMENTED_TOOLS: Final[frozenset[str]] = frozenset(
    {
        "ping",
        "load_mzml_summary",
        "list_spectra",
        "extract_chromatograms",
        "load_spectrum",
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
)

SERVER_COMMAND: Final = [sys.executable, "-c", "from msmcp.server import main; main()"]
PROTOCOL_VERSION: Final = "2025-06-18"
ANSWER_TIMEOUT_S: Final = 120.0


def _converse() -> tuple[dict[int, dict[str, Any]], int]:
    """Run initialize -> tools/list -> ping against a real server process."""
    requests: list[dict[str, Any]] = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "stdio-smoke", "version": "0"},
            },
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "ping", "arguments": {}},
        },
    ]

    proc = subprocess.Popen(
        SERVER_COMMAND,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    assert proc.stdin is not None and proc.stdout is not None

    lines: queue.Queue[str | None] = queue.Queue()

    def pump() -> None:
        for line in proc.stdout:  # type: ignore[union-attr]
            lines.put(line)
        lines.put(None)

    threading.Thread(target=pump, daemon=True).start()

    responses: dict[int, dict[str, Any]] = {}
    try:
        for request in requests:
            proc.stdin.write(json.dumps(request) + "\n")
        proc.stdin.flush()

        while len(responses) < 3:
            try:
                line = lines.get(timeout=ANSWER_TIMEOUT_S)
            except queue.Empty:
                pytest.fail(
                    f"no answer for {sorted({1, 2, 3} - set(responses))} within "
                    f"{ANSWER_TIMEOUT_S}s"
                )
            if line is None:
                pytest.fail(
                    f"the server closed stdout after answering {sorted(responses)}"
                )
            message: dict[str, Any] = json.loads(line)
            if "id" in message:
                assert "error" not in message, message
                responses[message["id"]] = message
    finally:
        proc.stdin.close()
        try:
            proc.wait(timeout=ANSWER_TIMEOUT_S)
        except subprocess.TimeoutExpired:  # pragma: no cover - only on a hung server
            proc.kill()
            raise

    return responses, proc.returncode


def test_the_stdio_server_serves_the_documented_tool_surface() -> None:
    """A host that launches the server gets the contract the docs promise."""
    responses, exit_code = _converse()

    handshake = responses[1]["result"]
    assert handshake["protocolVersion"] == PROTOCOL_VERSION
    # The advertised version is the installed distribution's, so a stale
    # hard-coded string or an unbuilt checkout is caught here.
    assert handshake["serverInfo"]["version"] == version("msmcp")

    advertised = {tool["name"] for tool in responses[2]["result"]["tools"]}
    assert advertised == set(DOCUMENTED_TOOLS), (
        f"undocumented: {sorted(advertised - DOCUMENTED_TOOLS)} "
        f"missing: {sorted(DOCUMENTED_TOOLS - advertised)}"
    )

    ping = responses[3]["result"]
    assert not ping.get("isError"), ping
    assert "operational" in ping["content"][0]["text"]

    # Closing stdin is the shutdown signal, and it must be clean.
    assert exit_code == 0
