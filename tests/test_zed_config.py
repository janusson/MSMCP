"""Guards for the editor-facing MCP launch configuration.

``.zed/settings.json`` is the exact launch line Zed runs when it spawns MSMCP
as a child process, and ``README.md`` publishes the same block for other
hosts.  Both must use Zed's current ``command``/``args`` schema: the nested
``command: {"path": ..., "args": ...}`` object is the pre-2025-11-25 form and
is rewritten by Zed's settings migration on load, so a repo that ships it
depends on undocumented migration behaviour.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
ZED_SETTINGS = REPO_ROOT / ".zed" / "settings.json"
README = REPO_ROOT / "README.md"

EXPECTED_ARGV = ["uv", "run", "msmcp"]


def _json_objects_in_fences(text: str) -> list[dict[str, Any]]:
    """Return every ``json`` fenced block in *text* that parses as an object."""
    objects: list[dict[str, Any]] = []
    for chunk in text.split("```json")[1:]:
        body = chunk.split("```", 1)[0]
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            objects.append(parsed)
    return objects


def zed_server_argv() -> list[str]:
    """Return the argv Zed spawns, asserting the schema is the current one."""
    settings = json.loads(ZED_SETTINGS.read_text(encoding="utf-8"))
    entry = settings["context_servers"]["msmcp"]
    command = entry["command"]
    assert isinstance(command, str), (
        "`.zed/settings.json` must use Zed's current schema - a `command` "
        "string plus an `args` array (https://zed.dev/docs/ai/mcp). Found the "
        f"pre-2025-11-25 nested object form: {command!r}"
    )
    args = entry["args"]
    assert isinstance(args, list), f"`args` must be a list, got {args!r}"
    return [command, *args]


def test_zed_config_uses_flat_command_schema() -> None:
    """The launch line is exactly `uv run msmcp`, in the current Zed schema."""
    assert zed_server_argv() == EXPECTED_ARGV


def test_readme_zed_snippet_matches_settings_file() -> None:
    """README's published Zed block cannot drift from the shipped file."""
    documented = [
        obj
        for obj in _json_objects_in_fences(README.read_text(encoding="utf-8"))
        if "context_servers" in obj
    ]
    assert len(documented) == 1, (
        f"expected exactly one context_servers block in README.md, got {len(documented)}"
    )
    assert documented[0] == json.loads(ZED_SETTINGS.read_text(encoding="utf-8"))


@pytest.mark.skipif(shutil.which("uv") is None, reason="uv is not on PATH")
def test_configured_argv_serves_mcp_over_stdio() -> None:
    """The configured argv really spawns a working stdio MCP server."""
    proc = subprocess.Popen(
        zed_server_argv(),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert proc.stdin is not None and proc.stdout is not None
    assert proc.stderr is not None
    try:
        _handshake(proc)
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=30)


def _handshake(proc: subprocess.Popen[str]) -> None:
    """Drive ``initialize`` + ``tools/list`` and assert the server is live."""
    requests = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "zed-config-test", "version": "0.0.1"},
            },
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
    ]

    # stderr is drained concurrently: the server logs at INFO and a full pipe
    # buffer would deadlock the child mid-session (same reason as
    # tests/test_smoke_stdio.py).
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

    assert proc.stdin is not None
    for request in requests:
        proc.stdin.write(json.dumps(request) + "\n")
    proc.stdin.flush()

    deadline = time.monotonic() + 60.0
    responses: list[dict[str, Any]] = []
    while time.monotonic() < deadline:
        # Parsing fails loudly on any non-JSON stdout line, i.e. on stray logs.
        responses = [json.loads(line) for line in stdout_lines if line.strip()]
        if len(responses) >= 2:
            break
        time.sleep(0.05)
    else:
        pytest.fail(
            f"no server responses within 60s: stdout={stdout_lines!r} "
            f"stderr={stderr_lines[-5:]!r}"
        )

    by_id = {response.get("id"): response for response in responses}
    init = by_id.get(1)
    assert init is not None, f"no response to initialize: {responses!r}"
    assert "error" not in init, init
    assert init["result"]["serverInfo"]["name"] == "MSMCP-MassFlow-Adapter"

    listed = by_id.get(2)
    assert listed is not None, f"no response to tools/list: {responses!r}"
    assert "error" not in listed, listed
    assert listed["result"]["tools"], "tools/list returned no tools"

    assert proc.stdin is not None
    proc.stdin.close()
    proc.wait(timeout=30)
    for reader in readers:
        reader.join(timeout=5)
    assert proc.returncode == 0, f"server exited {proc.returncode}: {stderr_lines[-5:]}"
