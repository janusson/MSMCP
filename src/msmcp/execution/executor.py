"""The :class:`JobExecutor` interface and its local asyncio implementation.

Why an interface at all
-----------------------
The MCP server must be able to start work that outlives one ``tools/call``.
That requirement is stable; *how* the work executes is not.  Tools therefore
depend on three operations - submit, status, cancel - and never on the
mechanism.  v1.0 ships one implementation
(:class:`LocalAsyncExecutor`); a process-based, distributed or Prefect-backed
implementation can be substituted without touching tool code.

MCP Tasks compatibility
-----------------------
Statuses are the MCP task vocabulary (:data:`mcp.types.TaskStatus`), and
:meth:`JobStatusSnapshot.to_task_dict` renders a snapshot into the shape of an
MCP ``Task`` (``task_id``, ``status``, ``status_message``, ``created_at``,
``last_updated_at``, ``ttl``, ``poll_interval``).

Note that the installed ``mcp`` SDK (2.1.1) defines the Tasks types but does
**not** dispatch ``tasks/get``, ``tasks/result`` or ``tasks/cancel``: they are
absent from the request unions, so a server cannot yet answer them.  MSMCP
therefore exposes the same semantics through its own polling tool and keeps the
wire representation aligned so that, once the transport dispatches Tasks, the
mapping is mechanical rather than a redesign.  The alternative - inventing a
custom protocol - would be incompatible with the specification and is
deliberately not done.

Cancellation semantics
----------------------
Work runs on a worker thread, and a thread cannot be interrupted.  Cancelling
therefore marks the job cancelled and discards its result: the caller observes
a terminal ``cancelled`` state and never sees partial output, while the worker
thread is left to unwind on its own.
"""

from __future__ import annotations

import asyncio
import logging
import time
import traceback
import uuid
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final, Literal

from mcp.types import TaskStatus

from msmcp.errors import MsmcpError

logger = logging.getLogger("msmcp.execution")

__all__ = [
    "DEFAULT_JOB_TTL_SECONDS",
    "DEFAULT_MAX_CONCURRENCY",
    "ExecutorError",
    "JobExecutor",
    "JobHandle",
    "JobPhase",
    "JobResult",
    "JobStatusSnapshot",
    "LocalAsyncExecutor",
    "UnknownJobError",
]

DEFAULT_JOB_TTL_SECONDS: Final[float] = 3600.0
"""How long a finished job is retained so its outcome can still be collected."""

DEFAULT_MAX_CONCURRENCY: Final[int] = 4
"""How many jobs an executor runs at once.

A job is started by an LLM client, so an eager or misbehaving agent could
otherwise spawn unbounded worker threads and saturate the CPU.  Jobs beyond the
cap wait in the ``queued`` phase.
"""

DEFAULT_POLL_INTERVAL_MS: Final[int] = 500
"""Suggested client polling interval, mirroring ``Task.poll_interval``."""

JobPhase = Literal["queued", "running", "settled"]
"""Finer-grained progress than :data:`~mcp.types.TaskStatus` provides.

``TaskStatus`` collapses "waiting to start" and "executing" into ``working``;
the phase keeps that distinction available for a human-readable status line
without inventing a non-standard status vocabulary.
"""

_TERMINAL_STATUSES: frozenset[str] = frozenset({"completed", "failed", "cancelled"})


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------
class ExecutorError(MsmcpError):
    """Base class for job-execution failures."""


class UnknownJobError(ExecutorError):
    """Raised when a job ID names no job the executor knows about."""


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class JobHandle:
    """The immediate receipt returned by :meth:`JobExecutor.submit`."""

    job_id: str
    operation: str
    created_at: float

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe mapping."""
        return {
            "job_id": self.job_id,
            "operation": self.operation,
            "created_at": _iso(self.created_at),
        }


@dataclass(frozen=True, slots=True)
class JobStatusSnapshot:
    """A point-in-time view of one job, shaped like an MCP ``Task``."""

    job_id: str
    operation: str
    status: TaskStatus
    phase: JobPhase
    status_message: str | None
    created_at: float
    last_updated_at: float
    ttl_seconds: float | None = None
    poll_interval_ms: int | None = DEFAULT_POLL_INTERVAL_MS

    @property
    def is_terminal(self) -> bool:
        """Whether the job can no longer change state."""
        return self.status in _TERMINAL_STATUSES

    def to_task_dict(self) -> dict[str, Any]:
        """Render this snapshot in the shape of an MCP ``Task``.

        Aligned with the specification so that a transport that dispatches
        ``tasks/get`` can serve this without a translation layer.
        """
        return {
            "task_id": self.job_id,
            "status": self.status,
            "status_message": self.status_message,
            "created_at": _iso(self.created_at),
            "last_updated_at": _iso(self.last_updated_at),
            "ttl": _ms(self.ttl_seconds),
            "poll_interval": self.poll_interval_ms,
        }


@dataclass(frozen=True, slots=True)
class JobResult:
    """The collected outcome of a job.

    ``value`` is populated only for a completed job; ``error`` and
    ``traceback`` only for a failed one.  Both are ``None`` while the job is
    still running, which is what lets a poller answer "not yet" without
    treating it as a failure.
    """

    job_id: str
    status: TaskStatus
    phase: JobPhase
    value: Any | None = None
    error: str | None = None
    traceback: str | None = None


@dataclass(slots=True)
class _Record:
    """Mutable bookkeeping for one submitted job."""

    job_id: str
    operation: str
    parameters: Mapping[str, Any]
    created_at: float
    updated_at: float
    status: TaskStatus = "working"
    phase: JobPhase = "queued"
    status_message: str | None = "queued"
    value: Any | None = None
    error: str | None = None
    traceback: str | None = None
    ttl_seconds: float | None = None
    deadline: float | None = None
    task: asyncio.Task[None] | None = None
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)


def _iso(timestamp: float) -> str:
    """Render a POSIX timestamp as an ISO 8601 UTC string."""
    return datetime.fromtimestamp(timestamp, tz=UTC).isoformat()


def _ms(seconds: float | None) -> int | None:
    """Convert seconds to whole milliseconds, preserving ``None``."""
    return None if seconds is None else int(seconds * 1000)


def _summarise_exception(exc: BaseException) -> str:
    """Render a one-line description of *exc*, never an empty string."""
    text = str(exc).strip()
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


# ---------------------------------------------------------------------------
# Interface
# ---------------------------------------------------------------------------
class JobExecutor(ABC):
    """Submit, poll and cancel long-running operations.

    Implementations must be safe to call from the event loop that serves MCP
    requests.  They may be backed by threads, processes or a remote
    orchestrator; callers must not assume any particular one.
    """

    @abstractmethod
    def submit(
        self,
        operation: str,
        fn: Callable[[], Any],
        *,
        parameters: Mapping[str, Any] | None = None,
        ttl_seconds: float | None = None,
    ) -> JobHandle:
        """Start *fn* in the background and return a handle immediately.

        ``fn`` must be a nullary callable.  It is executed off the event loop,
        so it may block.  ``parameters`` is retained for diagnostics and
        provenance only; it is never passed to *fn*.
        """

    @abstractmethod
    def status(self, job_id: str) -> JobStatusSnapshot:
        """Return the current state of *job_id*.

        Raises
        ------
        UnknownJobError
            If the executor has no record of *job_id* (never submitted,
            already forgotten, or lost with a previous process).
        """

    @abstractmethod
    def result(self, job_id: str) -> JobResult:
        """Return the outcome of *job_id*, which may still be pending."""

    @abstractmethod
    def cancel(self, job_id: str) -> JobStatusSnapshot:
        """Request cancellation and return the resulting state.

        Cancelling a job that already reached a terminal state is not an error;
        the unchanged snapshot is returned so a client can report the truth.
        """

    @abstractmethod
    def forget(self, job_id: str) -> bool:
        """Drop all record of *job_id*.  Returns ``False`` if it was absent."""

    @abstractmethod
    def list_jobs(self) -> tuple[JobStatusSnapshot, ...]:
        """Return snapshots for every job currently known."""

    @abstractmethod
    def shutdown(self) -> None:
        """Cancel outstanding work and release resources."""


# ---------------------------------------------------------------------------
# Local implementation
# ---------------------------------------------------------------------------
class LocalAsyncExecutor(JobExecutor):
    """Runs jobs on worker threads of the server's own event loop.

    CPU-bound work is dispatched with :func:`asyncio.to_thread`, so the MCP
    event loop stays responsive while a scan runs.  No external service,
    daemon or database is involved.

    The trade-off is explicit: records live in this object, so a job's outcome
    does not survive a server restart.  :meth:`status` raises
    :class:`UnknownJobError` for an unknown ID, and the polling tool reports
    that as a terminal failure rather than an ambiguous state a client could
    spin on forever.
    """

    def __init__(
        self,
        *,
        max_concurrency: int = DEFAULT_MAX_CONCURRENCY,
        default_ttl_seconds: float | None = DEFAULT_JOB_TTL_SECONDS,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        if max_concurrency <= 0:
            raise ValueError("max_concurrency must be positive")
        self._max_concurrency = max_concurrency
        self._default_ttl_seconds = default_ttl_seconds
        self._clock = clock
        self._monotonic = monotonic
        self._id_factory = id_factory or (lambda: uuid.uuid4().hex)
        self._records: dict[str, _Record] = {}
        self._semaphore: asyncio.Semaphore | None = None
        self._cleanup_tasks: set[asyncio.Task[None]] = set()

    # -- introspection ---------------------------------------------------
    @property
    def max_concurrency(self) -> int:
        """Configured ceiling on simultaneously running jobs."""
        return self._max_concurrency

    def __len__(self) -> int:
        return len(self._records)

    def _limits(self) -> asyncio.Semaphore:
        """Return the concurrency semaphore, creating it on first submission.

        The semaphore is bound to the running event loop, so it cannot be built
        in ``__init__``.
        """
        if self._semaphore is None:
            self._semaphore = asyncio.Semaphore(self._max_concurrency)
        return self._semaphore

    def _record(self, job_id: str) -> _Record:
        record = self._records.get(job_id)
        if record is None:
            raise UnknownJobError(
                f"No job {job_id!r} is known to this executor.  Job state is "
                f"held in memory, does not survive a server restart, and may "
                f"have been forgotten after its retention period."
            )
        return record

    def _snapshot(self, record: _Record) -> JobStatusSnapshot:
        return JobStatusSnapshot(
            job_id=record.job_id,
            operation=record.operation,
            status=record.status,
            phase=record.phase,
            status_message=record.status_message,
            created_at=record.created_at,
            last_updated_at=record.updated_at,
            ttl_seconds=record.ttl_seconds,
        )

    # -- submitting ------------------------------------------------------
    def submit(
        self,
        operation: str,
        fn: Callable[[], Any],
        *,
        parameters: Mapping[str, Any] | None = None,
        ttl_seconds: float | None = None,
    ) -> JobHandle:
        """Start *fn* on a worker thread and return its handle immediately."""
        if not callable(fn):
            raise TypeError("fn must be callable")
        ttl = ttl_seconds if ttl_seconds is not None else self._default_ttl_seconds
        now = self._clock()
        record = _Record(
            job_id=self._id_factory(),
            operation=operation,
            parameters=dict(parameters or {}),
            created_at=now,
            updated_at=now,
            ttl_seconds=ttl,
            deadline=(self._monotonic() + ttl) if ttl is not None else None,
        )
        self._records[record.job_id] = record
        record.task = asyncio.create_task(self._run(record, fn))
        logger.info("Submitted job %s (%s)", record.job_id, operation)
        return JobHandle(
            job_id=record.job_id,
            operation=record.operation,
            created_at=record.created_at,
        )

    async def _run(self, record: _Record, fn: Callable[[], Any]) -> None:
        """Execute *fn* under the concurrency cap and record the outcome."""
        try:
            if record.cancel_event.is_set():
                self._settle(record, "cancelled", "cancelled")
                return
            async with self._limits():
                if record.cancel_event.is_set():
                    self._settle(record, "cancelled", "cancelled")
                    return
                record.phase = "running"
                record.status_message = "running"
                record.updated_at = self._clock()
                value = await asyncio.to_thread(fn)
                if record.cancel_event.is_set():
                    self._settle(record, "cancelled", "cancelled")
                    return
                record.value = value
                self._settle(record, "completed", "completed")
        except asyncio.CancelledError:
            self._settle(record, "cancelled", "cancelled")
            raise
        except Exception as exc:
            logger.exception("Job %s failed", record.job_id)
            record.error = _summarise_exception(exc)
            record.traceback = "".join(
                traceback.format_exception(type(exc), exc, exc.__traceback__)
            )
            self._settle(record, "failed", record.error)
        finally:
            record.task = None
            self._schedule_expiry(record)

    def _settle(self, record: _Record, status: TaskStatus, message: str) -> None:
        """Move *record* to a terminal state and stamp the update time."""
        record.status = status
        record.phase = "settled"
        record.status_message = message
        record.updated_at = self._clock()

    # -- retention -------------------------------------------------------
    def _schedule_expiry(self, record: _Record) -> None:
        """Retain *record* for its TTL, then drop it.

        A finished job is kept briefly so its outcome can still be collected;
        afterwards it is removed so a long-running server never accumulates
        unbounded state.
        """
        if record.ttl_seconds is None:
            return
        task = asyncio.create_task(
            self._expire_later(record.job_id, record.ttl_seconds)
        )
        self._cleanup_tasks.add(task)
        task.add_done_callback(self._cleanup_tasks.discard)

    async def _expire_later(self, job_id: str, delay_seconds: float) -> None:
        try:
            await asyncio.sleep(delay_seconds)
        except asyncio.CancelledError:
            raise
        if self._records.pop(job_id, None) is not None:
            logger.info("Expired finished job %s from the record store", job_id)

    # -- polling ---------------------------------------------------------
    def status(self, job_id: str) -> JobStatusSnapshot:
        """Return the current state of *job_id*."""
        return self._snapshot(self._record(job_id))

    def result(self, job_id: str) -> JobResult:
        """Return the outcome of *job_id*, or a non-terminal placeholder."""
        record = self._record(job_id)
        return JobResult(
            job_id=record.job_id,
            status=record.status,
            phase=record.phase,
            value=record.value,
            error=record.error,
            traceback=record.traceback,
        )

    def list_jobs(self) -> tuple[JobStatusSnapshot, ...]:
        """Return snapshots for every job currently known."""
        return tuple(self._snapshot(record) for record in self._records.values())

    # -- cancellation and teardown ---------------------------------------
    def cancel(self, job_id: str) -> JobStatusSnapshot:
        """Request cancellation; returns the resulting state.

        A thread already executing cannot be interrupted, so the job is marked
        cancelled immediately and any late result is discarded.  This keeps the
        caller-visible contract honest: ``cancelled`` never carries output.
        """
        record = self._record(job_id)
        if record.status in _TERMINAL_STATUSES:
            logger.info(
                "Cancellation ignored for job %s: already %s",
                record.job_id,
                record.status,
            )
            return self._snapshot(record)

        record.cancel_event.set()
        if record.task is not None and not record.task.done():
            record.task.cancel()
        self._settle(record, "cancelled", "cancelled")
        logger.info("Cancellation requested for job %s", record.job_id)
        return self._snapshot(record)

    def forget(self, job_id: str) -> bool:
        """Drop all record of *job_id*."""
        record = self._records.pop(job_id, None)
        if record is None:
            return False
        if record.task is not None and not record.task.done():
            record.cancel_event.set()
            record.task.cancel()
        return True

    def purge_expired(self) -> int:
        """Drop finished jobs whose retention window has elapsed.

        Expiry is normally driven by the timer scheduled at completion; this
        method makes it also reachable synchronously for tests and for callers
        that want an explicit sweep.
        """
        now = self._monotonic()
        doomed = [
            job_id
            for job_id, record in self._records.items()
            if record.deadline is not None
            and record.phase == "settled"
            and now >= record.deadline
        ]
        for job_id in doomed:
            self._records.pop(job_id, None)
        return len(doomed)

    def shutdown(self) -> None:
        """Cancel outstanding work and drop every record."""
        for record in list(self._records.values()):
            if record.task is not None and not record.task.done():
                record.cancel_event.set()
                record.task.cancel()
        for task in list(self._cleanup_tasks):
            task.cancel()
        self._cleanup_tasks.clear()
        self._records.clear()
