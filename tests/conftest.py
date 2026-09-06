"""Shared fixtures for the MSMCP test suite.

The MCP tools are defined as closures inside each tool module's
``register_tools(mcp)`` function.  The test suite registers them against a
minimal stand-in for the current ``MCPServer`` (the real SDK only receives a
``tool()`` decorator call) and exposes the captured callables as fixtures.
"""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable

import pytest

# Pin embedding backends to the deterministic mocks for hermetic tests: no
# torch / DreaMS imports, no weight downloads, no network.  Adapter tests
# that exercise the real-inference pipeline inject stub models explicitly.
os.environ["MSMCP_EMBEDDING_BACKEND"] = "mock"

from msmcp.tools import chem, search, similarity


class FakeMCP[T]:
    """Minimal stand-in for ``MCPServer`` that captures registered tools."""

    def __init__(self) -> None:
        self.tools: dict[str, Callable[..., T]] = {}

    def tool(self) -> Callable[[Callable[..., T]], Callable[..., T]]:
        """Return a decorator that records *fn* and returns it unchanged."""

        def decorator(fn: Callable[..., T]) -> Callable[..., T]:
            self.tools[fn.__name__] = fn
            return fn

        return decorator


@pytest.fixture(scope="session")
def chem_tools() -> dict[str, Callable[..., str]]:
    """Cheminformatics tools keyed by name (``predict_adduct_offset``, ...)."""
    mcp = FakeMCP[str]()
    chem.register_tools(mcp)
    return mcp.tools


@pytest.fixture(scope="session")
def sim_tools() -> dict[str, Callable[..., str]]:
    """Similarity/validation tools keyed by name (``validate_precursor``, ...)."""
    mcp = FakeMCP[str]()
    similarity.register_tools(mcp)
    return mcp.tools


@pytest.fixture(scope="session")
def search_tools() -> dict[str, Callable[..., Awaitable[str]]]:
    """Library-search tools keyed by name (``search_library``, ...)."""
    mcp = FakeMCP[Awaitable[str]]()
    search.register_tools(mcp)
    return mcp.tools
