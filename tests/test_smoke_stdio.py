"""End-to-end smoke test of the MCPServer over a real stdio transport.

Launches ``python -m msmcp.server`` as a child process and drives a JSON-RPC
session over the stdio pipe: the ``initialize`` handshake,
``notifications/initialized``, ``tools/list``, and a ``tools/call`` on
``ping``.

A reader thread drains stdout while a main-thread loop waits for the expected
responses; stdin is only closed once every response has been received.
Closing stdin at EOF shuts the server down, which can drop an in-flight final
response — a real MCP host keeps the pipe open for the whole session, so this
test must do the same.

This is the only test that exercises the real transport.  Everything it
asserts is invisible to the in-process suite: the handshake negotiates the
requested protocol version, every tool is registered and documented on the
wire, ``ping`` reports operational, the server exits cleanly once stdin closes,
and — critically — **every stdout line is a well-formed JSON-RPC response**.
Any stray logging on stdout would corrupt the framing and fail the JSON parse,
which is what makes this the guard for the "never write to stdout on stdio"
rule.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from typing import Any

import pytest

EXPECTED_RESPONSES = 3  # initialize, tools/list, tools/call (notifications have none)

EXPECTED_TOOLS = {
    "ping",
    "load_mzml_summary",
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


def test_stdio_jsonrpc_session() -> None:
    proc = subprocess.Popen(
        [sys.executable, "-m", "msmcp.server"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert proc.stdin is not None and proc.stdout is not None
    assert proc.stderr is not None

    try:
        _run_session(proc)
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=30)


def _run_session(proc: subprocess.Popen[str]) -> None:
    msgs = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "smoke-test", "version": "0.0.1"},
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

    # --- drain both pipes on reader threads ---------------------------------
    # stderr must be drained concurrently, not read after exit: the server logs
    # at INFO, and a full pipe buffer would deadlock the child mid-session.
    stdout_lines: list[str] = []
    stderr_lines: list[str] = []

    def _drain(stream: Any, sink: list[str]) -> None:
        for line in stream:
            sink.append(line)

    readers = [
        threading.Thread(target=_drain, args=(proc.stdout, stdout_lines), daemon=True),
        threading.Thread(target=_drain, args=(proc.stderr, stderr_lines), daemon=True),
    ]
    for reader in readers:
        reader.start()

    # --- write the session; keep stdin open until all responses arrive ------
    assert proc.stdin is not None
    for m in msgs:
        proc.stdin.write(json.dumps(m) + "\n")
    proc.stdin.flush()

    deadline = time.monotonic() + 60.0
    responses: list[dict[str, Any]] = []
    while time.monotonic() < deadline:
        # Parsing fails loudly on any non-JSON line, i.e. on stray stdout output.
        responses = [json.loads(line) for line in stdout_lines if line.strip()]
        if len(responses) >= EXPECTED_RESPONSES:
            break
        time.sleep(0.05)
    else:
        pytest.fail(
            "timeout waiting for server responses; "
            f"stdout={stdout_lines!r} stderr={stderr_lines[-5:]!r}"
        )

    assert proc.stdin is not None
    proc.stdin.close()
    proc.wait(timeout=30)
    for reader in readers:
        reader.join(timeout=5)

    # --- initialization handshake ------------------------------------------
    by_id = {resp.get("id"): resp for resp in responses}
    init_resp = by_id.get(1)
    assert init_resp is not None, "no response to initialize"
    assert "error" not in init_resp, init_resp
    init_result = init_resp["result"]
    assert init_result.get("protocolVersion") == "2025-06-18", init_result
    assert init_result.get("serverInfo", {}).get("name") == "MSMCP-MassFlow-Adapter"
    assert "tools" in init_result.get("capabilities", {}), init_result

    # --- tools/list: exactly the ten registered tools ----------------------
    list_resp = by_id.get(2)
    assert list_resp is not None, "no response to tools/list"
    assert "error" not in list_resp, list_resp
    tool_names = {t["name"] for t in list_resp["result"]["tools"]}
    assert tool_names == EXPECTED_TOOLS, f"tool mismatch: {EXPECTED_TOOLS ^ tool_names}"

    # --- every tool reaches the host documented ----------------------------
    for tool in list_resp["result"]["tools"]:
        assert tool.get("description"), f"{tool['name']} has no description"
        for param, spec in tool.get("inputSchema", {}).get("properties", {}).items():
            assert spec.get("description"), (
                f"{tool['name']}.{param} reaches the host with no description"
            )

    # --- tools/call: ping reports operational ------------------------------
    ping_resp = by_id.get(3)
    assert ping_resp is not None, "no response to ping tools/call"
    assert "error" not in ping_resp, ping_resp
    assert "operational" in json.dumps(ping_resp["result"])

    assert proc.returncode == 0, f"server exited {proc.returncode}: {stderr_lines[-5:]}"
