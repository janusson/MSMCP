"""MSMCP-MassFlow-Adapter: MCP server bridging mass spectrometry tooling to LLM hosts."""

import logging
import sys

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

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
mcp = MCPServer(
    "MSMCP-MassFlow-Adapter",
    version="0.1.0",
    instructions=(
        "Local computational mass-spectrometry toolkit.  Its tools read MS "
        "acquisitions from the filesystem (confined to the server's "
        "configured allowed root) and answer questions about them: file "
        "summaries, QC metrics, exact-mass and isotope calculations, "
        "precursor-mass validation, and spectral similarity.\n\n"
        "Supported input formats: .mzML/.mzML.gz, .mgf and .imzML (imaging "
        "data, read through MassFlow).  The reader is chosen from the file "
        "extension.\n\n"
        "Large data never needs to enter the conversation.  load_spectrum "
        "parses a spectrum and returns a short server-side reference "
        "('ptr:spectrum:...'); pass that reference to search_library, inspect "
        "it with summarise_reference, and free it with release_reference.  "
        "References are held in memory and expire after a retention period.\n\n"
        "Typical flow: inspect an acquisition with load_mzml_summary or "
        "generate_qc_summary, then test a hypothesis with compute_cosine or "
        "validate_precursor.\n\n"
        "search_library is asynchronous: it submits the scan to a job "
        "executor, returns a job_id immediately, and you must poll "
        "check_search_status until the job leaves the queued/running state.\n\n"
        "Caveat: search_library still scans a synthetic in-memory library "
        "rather than the library file you name; only the query spectrum can "
        "be real data.  Treat its reports as pipeline demonstrations, never "
        "as compound identifications; the reports repeat this warning."
    ),
)


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
# Tools
# ---------------------------------------------------------------------------
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
