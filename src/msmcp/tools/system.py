"""Server-level diagnostic tools.

``ping`` was originally defined in :mod:`msmcp.server` and registered at module
scope, with a bare ``@mcp.tool()`` on the module-level ``MCPServer`` instance.
That made it the only tool outside the ``register_tools(mcp)`` convention every
other tool module follows, and every guard that discovers the tool surface by
calling ``register_tools`` therefore missed it: the evaluation notebook's
``EXPECTED_TOOLS``/``TOOLS`` cross-check, ``tests/test_eval_notebook.py``'s
"a registered tool is never exercised" assertion, and ``test_tool_schemas.py``'s
signature/model mapping.  It lives here so the convention is total and those
guards can see the whole surface rather than all-but-one of it.
"""

from __future__ import annotations

import logging
from typing import Any

from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

logger = logging.getLogger("msmcp.tools.system")


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------
class PingResponse(BaseModel):
    """Response schema for the diagnostic ping tool."""

    status: str = Field(description="'ok' when the server is operational.")
    message: str = Field(description="Human-readable status message.")
    massflow_available: bool = Field(
        description="True when the optional `massflow` backend imported."
    )
    massflow_version: str | None = Field(
        default=None,
        description="Installed MassFlow version, or null when it is absent.",
    )


# ---------------------------------------------------------------------------
# Public registration
# ---------------------------------------------------------------------------
def register_tools(mcp: Any) -> None:
    """Register the server-level diagnostic tools on the MCPServer instance."""

    @mcp.tool(
        title="Server health check",
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False),
    )
    def ping() -> PingResponse:
        """Check that the server is running and its dependencies import.

        Takes no arguments.  Reports whether `massflow` is available and which
        version is installed.  Call it first when another MSMCP tool fails in an
        unexpected way, to tell a broken environment apart from bad arguments.
        """
        from msmcp.massflow_io import massflow_version

        version = massflow_version()
        massflow_available = version is not None

        logger.info("ping() invoked; massflow_available=%s", massflow_available)

        return PingResponse(
            status="ok",
            message="MSMCP-MassFlow-Adapter is operational.",
            massflow_available=massflow_available,
            massflow_version=version,
        )
