"""Server-level prose, and the upstream assumption it rests on.

Both of these have already drifted once, and neither had a test:

* the ``instructions`` string ``MCPServer`` hands a host on ``initialize``.  It
  is the first thing a model is told about this server, and it spent a release
  telling the model that ``search_library`` could not read a library from disk
  after the MSP/NIST reader had shipped — so the capability existed and the
  model was instructed not to use it.
* the assumption that the installed ``mcp`` SDK does not dispatch ``tasks/*``.
  ``ARCHITECTURE.md`` and ``README.md`` both justify MSMCP's own polling tool
  with that claim, and nothing tested it.
"""

from __future__ import annotations

import os
from typing import get_args

import mcp.types as mcp_types
import pytest

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
# The upstream assumption the docs rest on
# ---------------------------------------------------------------------------
#: Tasks request types the installed SDK defines but does not dispatch.
#: ARCHITECTURE.md's "MCP Tasks: what is implemented and what is not" section
#: and README.md's two Tasks paragraphs both assert these are absent from the
#: request unions, which is the stated reason MSMCP exposes
#: ``check_search_status`` instead of answering Tasks natively.
TASK_REQUESTS = (
    mcp_types.GetTaskRequest,
    mcp_types.GetTaskPayloadRequest,
    mcp_types.CancelTaskRequest,
    mcp_types.ListTasksRequest,
)


@pytest.mark.parametrize("union_name", ["ClientRequest", "ServerRequest"])
def test_installed_sdk_does_not_dispatch_tasks(union_name: str) -> None:
    """Fails the day ``mcp`` adds ``tasks/*`` to its request unions.

    Deliberately a failure rather than a skip: this is the trigger for roadmap
    issue #7 ("waiting on the mcp SDK dispatching tasks/*").  When it goes red,
    decide whether MSMCP should answer Tasks natively, and update the documents
    that claim it cannot.
    """
    dispatched = set(get_args(getattr(mcp_types, union_name))) & set(TASK_REQUESTS)
    assert not dispatched, (
        f"{union_name} now dispatches {sorted(t.__name__ for t in dispatched)}: the "
        f"SDK can carry MCP Tasks, so ARCHITECTURE.md ('MCP Tasks: what is "
        f"implemented and what is not') and README.md's Tasks paragraphs need "
        f"revisiting, and roadmap #7 is unblocked"
    )
