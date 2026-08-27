"""Smoke-test the MCPServer over a real stdio transport.

Launches ``python -m msmcp.server`` and drives a JSON-RPC session over
stdio: the ``initialize`` handshake, ``notifications/initialized``,
``tools/list``, and a ``tools/call`` on ``ping``.

A reader thread drains stdout while a main-thread loop waits for the
expected responses; stdin is only closed once every response has been
received.  Closing stdin at EOF shuts the server down, which can drop an
in-flight final response — a real MCP host keeps the pipe open for the
whole session, so the smoke test must do the same.

Asserts that every stdout line is a well-formed JSON-RPC response (any
stray logging on stdout would break the framing and fail the JSON parse),
that the handshake negotiates the requested protocol version, that all 9
tools are registered, that ``ping`` reports operational, and that the
server exits cleanly once stdin closes.
"""

import json
import subprocess
import sys
import threading
import time

proc = subprocess.Popen(
    [sys.executable, "-m", "msmcp.server"],
    stdin=subprocess.PIPE,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    text=True,
)
assert proc.stdin is not None and proc.stdout is not None and proc.stderr is not None

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
EXPECTED_RESPONSES = 3  # initialize, tools/list, tools/call (notifications have none)

# --- drain stdout on a reader thread ------------------------------------
stdout_lines: list[str] = []


def _reader() -> None:
    assert proc.stdout is not None
    for line in proc.stdout:
        stdout_lines.append(line)


reader = threading.Thread(target=_reader, daemon=True)
reader.start()

# --- write the session; keep stdin open until all responses arrive ------
for m in msgs:
    proc.stdin.write(json.dumps(m) + "\n")
proc.stdin.flush()

deadline = time.monotonic() + 60.0
while time.monotonic() < deadline:
    responses = [json.loads(line) for line in stdout_lines if line.strip()]
    if len(responses) >= EXPECTED_RESPONSES:
        break
    time.sleep(0.05)
else:
    raise SystemExit("timeout waiting for server responses")

proc.stdin.close()
proc.wait(timeout=30)
reader.join(timeout=5)

stderr_tail = proc.stderr.read().strip().splitlines()[-3:]

print(f"server exit code: {proc.returncode}")
for resp in responses:
    # The initialize result is small — print it in full; truncate the rest.
    limit = 400 if resp.get("id") == 1 else 220
    print(f"  id={resp.get('id')} -> {json.dumps(resp)[:limit]}")

by_id = {resp.get("id"): resp for resp in responses}

# --- initialization handshake -----------------------------------------
init_resp = by_id.get(1)
assert init_resp is not None, "no response to initialize"
assert "error" not in init_resp, init_resp
init_result = init_resp["result"]
assert init_result.get("protocolVersion") == "2025-06-18", init_result
assert init_result.get("serverInfo", {}).get("name") == "MSMCP-MassFlow-Adapter"
assert "tools" in init_result.get("capabilities", {}), init_result

# --- tools/list: exactly the 9 registered tools ------------------------
list_resp = by_id.get(2)
assert list_resp is not None, "no response to tools/list"
assert "error" not in list_resp, list_resp

tool_names = {t["name"] for t in list_resp["result"]["tools"]}
print(f"tools registered ({len(tool_names)}): {sorted(tool_names)}")

expected = {
    "ping",
    "load_mzml_summary",
    "predict_adduct_offset",
    "annotate_isotopes",
    "validate_precursor",
    "compute_cosine",
    "generate_qc_summary",
    "search_library",
    "check_search_status",
}
assert tool_names == expected, f"tool mismatch: {expected ^ tool_names}"

# --- tools/call: ping reports operational ------------------------------
ping_resp = by_id.get(3)
assert ping_resp is not None, "no response to ping tools/call"
assert "error" not in ping_resp, ping_resp
ping_text = json.dumps(ping_resp["result"])
assert "operational" in ping_text, ping_text

assert proc.returncode == 0, f"server exited {proc.returncode}: {stderr_tail}"
print("SMOKE TEST PASSED")
