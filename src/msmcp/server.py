"""MSMCP-MassFlow-Adapter: MCP server bridging mass spectrometry tooling to LLM hosts."""

import logging
import sys

from mcp.server.mcpserver import MCPServer

from msmcp.provenance import package_version
from msmcp.tools.chem import register_tools as _register_chem_tools
from msmcp.tools.chromatograms import register_tools as _register_chromatogram_tools
from msmcp.tools.io import register_tools as _register_io_tools
from msmcp.tools.qc import register_tools as _register_qc_tools
from msmcp.tools.search import register_tools as _register_search_tools
from msmcp.tools.similarity import register_tools as _register_sim_tools
from msmcp.tools.system import register_tools as _register_system_tools

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
# Version
# ---------------------------------------------------------------------------
# Resolved from the installed distribution, for which pyproject.toml is the
# authority.  Everything that reports or logs a version reads this one value:
# a second literal here is how the advertised version and the startup log
# line ("v0.1.0") drifted apart in an earlier release.
SERVER_VERSION: str = package_version("msmcp") or "0.0.0+unknown"

# ---------------------------------------------------------------------------
# Server instance
# ---------------------------------------------------------------------------
mcp = MCPServer(
    "MSMCP-MassFlow-Adapter",
    version=SERVER_VERSION,
    instructions=(
        "Local computational mass-spectrometry toolkit.  Its tools read MS "
        "acquisitions from the filesystem (confined to the server's "
        "configured allowed root) and answer questions about them: file "
        "summaries, QC metrics, exact-mass and isotope calculations, "
        "precursor-mass validation, and spectral similarity.\n\n"
        "extract_chromatograms returns structured scan-level TIC, base-peak, "
        "and optional multiple XIC traces from mzML in one bounded pass.\n\n"
        "Supported input formats: .mzML/.mzML.gz, .mgf and .imzML (imaging, "
        "requires the optional imaging extra, not the analytical MassFlow "
        "engine); search_library additionally reads MSP/NIST text "
        "libraries (.msp, .msp.gz).\n\n"
        "Large data never needs to enter the conversation.  load_spectrum "
        "parses a spectrum and returns a short server-side reference "
        "('ptr:spectrum:...'); pass that reference to search_library, inspect "
        "it with summarise_reference, and free it with release_reference.  "
        "References are held in memory and expire after a retention period.\n\n"
        "Typical flow: inspect an acquisition with load_mzml_summary or "
        "generate_qc_summary, list its spectra with list_spectra to pick an "
        "MS2 query, then test a hypothesis with compute_cosine or "
        "validate_precursor.\n\n"
        "search_library is asynchronous: it submits the scan to a job "
        "executor, returns a job_id immediately, and you must poll "
        "check_search_status until the job leaves the queued/running state.\n\n"
        "Caveat: search_library's query spectrum is always real data.  When "
        "database_file names an MSP/NIST-style text library (.msp, .msp.gz) "
        "it is read from disk through the same security boundary as an "
        "acquisition and searched for real; any other path falls back to a "
        "labelled synthetic library.  Hits are candidate matches ranked by "
        "the stated scorer — not validated identifications.  The report "
        "names which library answered it."
    ),
)


# ---------------------------------------------------------------------------
# Register tools from sub-modules
# ---------------------------------------------------------------------------
_register_io_tools(mcp)
_register_chromatogram_tools(mcp)
_register_chem_tools(mcp)
_register_sim_tools(mcp)
_register_search_tools(mcp)
_register_qc_tools(mcp)
_register_system_tools(mcp)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main() -> None:
    """Launch the server on the stdio transport (child-process mode)."""
    logger.info(
        "Starting MSMCP-MassFlow-Adapter v%s on stdio transport", SERVER_VERSION
    )
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
