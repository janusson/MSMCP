"""Pluggable execution of long-running operations.

The MCP server needs to start work that outlives a single ``tools/call`` and
report on it afterwards.  *How* that work runs is a deployment decision, not
an architectural one, so the server depends only on the :class:`JobExecutor`
interface:

* :class:`~msmcp.execution.executor.LocalAsyncExecutor` - the default, running
  CPU-bound work on worker threads of the server's own event loop.
* A future process-based, distributed or Prefect-backed executor can implement
  the same three operations (submit / status / cancel) without any tool code
  changing.

Prefect is deliberately *not* a dependency: the interface is what makes it
possible to add later, and it is all v1.0 needs.

Statuses use the MCP task vocabulary (:data:`mcp.types.TaskStatus`) so a
snapshot can be rendered into an MCP ``Task`` without translation.
"""

from msmcp.execution.executor import (
    ExecutorError,
    JobExecutor,
    JobHandle,
    JobResult,
    JobStatusSnapshot,
    LocalAsyncExecutor,
    UnknownJobError,
)

__all__ = [
    "ExecutorError",
    "JobExecutor",
    "JobHandle",
    "JobResult",
    "JobStatusSnapshot",
    "LocalAsyncExecutor",
    "UnknownJobError",
]
