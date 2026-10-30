"""Server-level prose, and the upstream assumption it rests on.

Both of these have already drifted once, and neither had a test:

* the ``instructions`` string ``MCPServer`` hands a host on ``initialize``.  It
  is the first thing a model is told about this server, and it spent a release
  telling the model that ``search_library`` could not read a library from disk
  after the MSP/NIST reader had shipped — so the capability existed and the
  model was instructed not to use it.
* whether the installed ``mcp`` SDK exposes the generic extension hooks needed
  to register and advertise non-core request methods.
"""

from __future__ import annotations

import os
from typing import Any

import mcp.types as mcp_types
from mcp.server.lowlevel.server import Server
from pydantic import BaseModel

os.environ.setdefault("MSMCP_EMBEDDING_BACKEND", "mock")

from msmcp.library import MSP_SUFFIXES
from msmcp.server import mcp


# ---------------------------------------------------------------------------
# What the server tells the host model
# ---------------------------------------------------------------------------
def test_instructions_name_every_library_format_search_can_read() -> None:
    """A capability the model is not told about is one it will not use."""
    text = mcp.instructions or ""
    missing = [suffix for suffix in MSP_SUFFIXES if suffix not in text]
    assert not missing, (
        f"the server instructions never mention {missing}; search_library reads "
        f"{', '.join(MSP_SUFFIXES)}, and the host can only use what it is told about"
    )


def test_instructions_say_a_supported_library_is_read_from_disk() -> None:
    """The regression guarded here: the text said the library is always synthetic."""
    text = mcp.instructions or ""
    assert "read from disk" in text, (
        "the instructions no longer state that a supported library is read from "
        "disk, which contradicts msmcp.library — see the F1 finding in "
        "docs/audit/2026-09-22-standard-audit.md"
    )


# ---------------------------------------------------------------------------
# Extension support exposed by the installed SDK
# ---------------------------------------------------------------------------
class _ExtensionParams(BaseModel):
    """Minimal typed request body for exercising the SDK extension hook."""

    value: str


async def _extension_handler(
    ctx: Any, params: _ExtensionParams
) -> mcp_types.EmptyResult:
    del ctx, params
    return mcp_types.EmptyResult()


def test_installed_sdk_can_register_and_advertise_extension_requests() -> None:
    """Guard the SDK hooks MSMCP would use for non-core Tasks methods.

    MCP Tasks are an extension, so membership in the core request unions is not
    a readiness signal. This verifies that the installed server can register
    an extension method and advertise its extension capability. It does not
    claim MSMCP itself has implemented the Tasks adapter.
    """
    server = Server("extension-contract-test")
    register = getattr(server, "add_request_handler", None)
    assert callable(register), (
        "mcp Server must expose add_request_handler for extension methods"
    )

    method = "tasks/get"
    register(method, _ExtensionParams, _extension_handler)
    assert method in server._request_handlers

    extension_id = "io.modelcontextprotocol/tasks"
    server.extensions[extension_id] = {}
    capabilities = server.get_capabilities()
    assert capabilities.extensions is not None
    assert extension_id in capabilities.extensions
