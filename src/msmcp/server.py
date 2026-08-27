"""MSMCP-MassFlow-Adapter: MCP server bridging mass spectrometry tooling to LLM hosts."""

import logging
import sys

from mcp.server.mcpserver import MCPServer
from pydantic import BaseModel

from msmcp.tools.chem import register_tools as _register_chem_tools
from msmcp.tools.io import register_tools as _register_io_tools
from msmcp.tools.qc import register_tools as _register_qc_tools
from msmcp.tools.search import register_tools as _register_search_tools
from msmcp.tools.similarity import register_tools as _register_sim_tools

# ---------------------------------------------------------------------------
# Logging boundary - ALL diagnostic output MUST go to stderr.
# Writing anything to stdout will corrupt the JSON-RPC framing on the
# stdio transport and cause the host LLM to lose sync with the server.
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("msmcp")

# ---------------------------------------------------------------------------
# Server instance
# ---------------------------------------------------------------------------
mcp = MCPServer("MSMCP-MassFlow-Adapter", version="0.1.0")


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------
class PingResponse(BaseModel):
    """Response schema for the diagnostic ping tool."""

    status: str
    message: str
    massflow_available: bool


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------
@mcp.tool()
def ping() -> PingResponse:
    """Diagnostic health check - verifies the server is alive and can import massflow."""
    try:
        import massflow  # noqa: F401

        massflow_available = True
    except ImportError:
        massflow_available = False

    logger.info("ping() invoked; massflow_available=%s", massflow_available)

    return PingResponse(
        status="ok",
        message="MSMCP-MassFlow-Adapter is operational.",
        massflow_available=massflow_available,
    )


# ---------------------------------------------------------------------------
# Register tools from sub-modules
# ---------------------------------------------------------------------------
_register_io_tools(mcp)
_register_chem_tools(mcp)
_register_sim_tools(mcp)
_register_search_tools(mcp)
_register_qc_tools(mcp)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main() -> None:
    """Launch the server on the stdio transport (child-process mode)."""
    logger.info("Starting MSMCP-MassFlow-Adapter v0.1.0 on stdio transport")
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
