"""Tests for the pluggable job executor.

The executor is the abstraction that keeps the MCP server independent of how
long-running work is actually run, so these tests pin its contract: submit
returns immediately, status/result/cancel behave, work is bounded, results are
retained for a TTL, and a failure is reported with its traceback rather than
swallowed.
"""

from __future__ import annotations

import asyncio
import logging
import threading

import pytest

from msmcp.execution import (
    LocalAsyncExecutor,
    UnknownJobError,
)
from msmcp.execution.executor import JobExecutor


class FakeClock:
    """A wall clock and a monotonic clock the test advances by hand."""

    def __init__(self) -> None:
        self.wall = 1_700_000_000.0
        self.mono = 1_000.0

    def time(self) -> float:
        return self.wall

    def monotonic(self) -> float:
        return self.mono

    def advance(self, seconds: float) -> None:
        self.wall += seconds
        self.mono += seconds


def executor(**kwargs: object) -> LocalAsyncExecutor:
    clock = FakeClock()
    kwargs.setdefault("clock", clock.time)
    kwargs.setdefault("monotonic", clock.monotonic)
    kwargs.setdefault("id_factory", lambda: "job-" + str(next(counter)))
    built = LocalAsyncExecutor(**kwargs)  # type: ignore[arg-type]
    built.clock = clock  # type: ignore[attr-defined]
    return built


counter = iter(range(1000))


async def _wait_until_settled(
    exec_: LocalAsyncExecutor, job_id: str, timeout: float = 10.0
) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if exec_.status(job_id).is_terminal:
            return
        await asyncio.sleep(0.01)
    pytest.fail(f"job {job_id} never settled")


# ---------------------------------------------------------------------------
# Interface
# ---------------------------------------------------------------------------
class TestInterface:
    def test_local_executor_is_a_job_executor(self) -> None:
        assert isinstance(LocalAsyncExecutor(), JobExecutor)

    def test_job_executor_cannot_be_instantiated(self) -> None:
        with pytest.raises(TypeError):
            JobExecutor()  # type: ignore[abstract]

    @pytest.mark.parametrize(
        ("kwargs", "message"),
        [
            ({"max_concurrency": 0}, "max_concurrency"),
        ],
    )
    def test_invalid_configuration(self, kwargs: dict, message: str) -> None:
        with pytest.raises(ValueError, match=message):
            LocalAsyncExecutor(**kwargs)


# ---------------------------------------------------------------------------
# Submit / status / result
# ---------------------------------------------------------------------------
class TestSubmission:
    async def test_submit_returns_immediately_and_completes(self) -> None:
        exec_ = executor()
        handle = exec_.submit("op", lambda: 6 * 7)

        assert handle.operation == "op"
        assert handle.job_id == "job-0"
        await _wait_until_settled(exec_, handle.job_id)

        snapshot = exec_.status(handle.job_id)
        assert snapshot.status == "completed"
        assert snapshot.phase == "settled"
        assert snapshot.is_terminal
        assert exec_.result(handle.job_id).value == 42

    async def test_status_is_working_until_settled(self) -> None:
        exec_ = executor(max_concurrency=1)
        release = threading.Event()
        handle = exec_.submit("op", release.wait)
        assert exec_.status(handle.job_id).status == "working"
        release.set()
        await _wait_until_settled(exec_, handle.job_id)

    async def test_snapshot_renders_as_an_mcp_task(self) -> None:
        """The snapshot must map onto the MCP ``Task`` shape without translation."""
        exec_ = executor()
        handle = exec_.submit("op", lambda: 1)
        await _wait_until_settled(exec_, handle.job_id)

        task = exec_.status(handle.job_id).to_task_dict()
        assert task["task_id"] == handle.job_id
        assert task["status"] == "completed"
        assert task["created_at"].endswith("+00:00")
        assert task["last_updated_at"].endswith("+00:00")
        assert task["poll_interval"] == 500

    async def test_blocking_work_does_not_block_the_event_loop(self) -> None:
        exec_ = executor()
        ticks = 0

        def slow() -> str:
            threading.Event().wait(0.05)
            return "done"

        handle = exec_.submit("op", slow)
        while not exec_.status(handle.job_id).is_terminal:
            ticks += 1
            await asyncio.sleep(0.005)
        assert ticks > 0, "the event loop never got a turn while the worker ran"
        assert exec_.result(handle.job_id).value == "done"

    async def test_parameters_are_retained_for_diagnostics(self) -> None:
        exec_ = executor()
        handle = exec_.submit("op", lambda: None, parameters={"chunk_size": 500})
        await _wait_until_settled(exec_, handle.job_id)
        assert exec_.result(handle.job_id).status == "completed"

    def test_non_callable_is_rejected(self) -> None:
        exec_ = executor()
        with pytest.raises(TypeError, match="callable"):
            exec_.submit("op", "not callable")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Failure paths
# ---------------------------------------------------------------------------
class TestFailure:
    async def test_exception_becomes_a_failed_job_with_a_traceback(self) -> None:
        exec_ = executor()

        def boom() -> None:
            raise RuntimeError("synthetic library generation failure")

        handle = exec_.submit("op", boom)
        await _wait_until_settled(exec_, handle.job_id)

        outcome = exec_.result(handle.job_id)
        assert outcome.status == "failed"
        assert outcome.value is None
        assert "RuntimeError" in (outcome.error or "")
        assert "synthetic library generation failure" in (outcome.error or "")
        assert "Traceback" in (outcome.traceback or "")
        assert "boom" in (outcome.traceback or "")

    async def test_exception_without_a_message_is_still_described(self) -> None:
        exec_ = executor()

        def boom() -> None:
            raise RuntimeError

        handle = exec_.submit("op", boom)
        await _wait_until_settled(exec_, handle.job_id)
        assert exec_.result(handle.job_id).error == "RuntimeError"

    def test_unknown_job_raises(self) -> None:
        exec_ = executor()
        for call in (exec_.status, exec_.result, exec_.cancel):
            with pytest.raises(UnknownJobError, match="No job"):
                call("job-does-not-exist")

    async def test_result_of_a_running_job_is_not_an_error(self) -> None:
        """A poller must be able to ask 'not yet?' without catching an exception."""
        exec_ = executor(max_concurrency=1)
        release = threading.Event()
        handle = exec_.submit("op", release.wait)
        try:
            outcome = exec_.result(handle.job_id)
            assert outcome.status == "working"
            assert outcome.value is None
            assert outcome.error is None
        finally:
            release.set()
            await _wait_until_settled(exec_, handle.job_id)


# ---------------------------------------------------------------------------
# Cancellation
# ---------------------------------------------------------------------------
class TestCancellation:
    async def test_cancelling_a_queued_job_never_runs_it(self) -> None:
        exec_ = executor(max_concurrency=1)
        blocker = threading.Event()
        running = exec_.submit("blocker", blocker.wait)
        queued = exec_.submit("op", lambda: "should not run")

        snapshot = exec_.cancel(queued.job_id)
        assert snapshot.status == "cancelled"
        assert snapshot.is_terminal

        blocker.set()
        await _wait_until_settled(exec_, running.job_id)
        await asyncio.sleep(0.05)
        assert exec_.result(queued.job_id).value is None
        assert exec_.result(queued.job_id).status == "cancelled"

    async def test_cancelling_a_finished_job_reports_the_real_state(self) -> None:
        exec_ = executor()
        handle = exec_.submit("op", lambda: 1)
        await _wait_until_settled(exec_, handle.job_id)

        snapshot = exec_.cancel(handle.job_id)
        assert snapshot.status == "completed"
        assert exec_.result(handle.job_id).value == 1

    @pytest.mark.parametrize("finish", ["return", "raise", "forget", "shutdown"])
    async def test_cancelled_worker_holds_capacity(self, finish: str) -> None:
        exec_ = executor(max_concurrency=1)
        loop = asyncio.get_running_loop()
        started = asyncio.Event()
        release = threading.Event()
        skipped = threading.Event()

        def work() -> str:
            loop.call_soon_threadsafe(started.set)
            release.wait()
            if finish == "raise":
                raise RuntimeError("late failure")
            return "late result"

        first = exec_.submit("first", work)
        try:
            await asyncio.wait_for(started.wait(), 5)
            queued = exec_.submit("queued", skipped.set)
            await asyncio.sleep(0)  # Let the queued job reach semaphore acquisition.
            cancelled = exec_.cancel(first.job_id)
            assert cancelled.status == "cancelled"
            assert exec_.cancel(first.job_id) == cancelled
            if finish == "shutdown":
                exec_.shutdown()
                assert exec_.list_jobs() == ()
            else:
                exec_.cancel(queued.job_id)
                if finish == "forget":
                    assert exec_.forget(first.job_id)
            second = exec_.submit("second", lambda: "ok")
            await asyncio.sleep(
                0
            )  # Run the newly submitted task up to its first await.
            assert exec_.status(second.job_id).phase == "queued"
        finally:
            release.set()

        await _wait_until_settled(exec_, second.job_id)
        assert exec_.result(second.job_id).value == "ok"
        assert not skipped.is_set()
        if finish in {"return", "raise"}:
            assert exec_.status(first.job_id) == cancelled
            outcome = exec_.result(first.job_id)
            assert outcome.value is outcome.error is outcome.traceback is None

    async def test_cancel_before_worker_starts_expires_under_ttl(self) -> None:
        """A job cancelled before its task first runs is still retired by TTL.

        Cancelling a task that has not started means its coroutine body never
        runs, so expiry cannot be scheduled from inside the job; ``cancel``
        has to start retention itself.
        """
        exec_ = executor(default_ttl_seconds=0.0)
        ran = threading.Event()
        handle = exec_.submit("op", ran.set)
        snapshot = exec_.cancel(handle.job_id)  # no await: the task never started
        assert snapshot.status == "cancelled"
        assert snapshot.phase == "settled"

        for _ in range(100):  # bounded event-loop turns, no wall-clock sleep
            if len(exec_) == 0:
                break
            await asyncio.sleep(0)
        assert len(exec_) == 0
        with pytest.raises(UnknownJobError):
            exec_.status(handle.job_id)
        assert not ran.is_set()

    @pytest.mark.parametrize("finish", ["return", "raise"])
    async def test_cancelled_terminal_snapshot_is_unchanged_after_worker_exits(
        self, finish: str, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The worker exiting after public cancellation changes nothing visible.

        The clock is advanced between cancellation and worker exit, so any
        re-settlement would change ``last_updated_at``.  A late failure is
        logged at debug level by exception type only.
        """
        caplog.set_level(logging.DEBUG, logger="msmcp.execution")
        exec_ = executor(max_concurrency=1)
        loop = asyncio.get_running_loop()
        entered = asyncio.Event()
        exited = asyncio.Event()
        release = threading.Event()
        secret = "sensitive detail /private/sample.mzML"

        def work() -> str:
            loop.call_soon_threadsafe(entered.set)
            try:
                release.wait()
                if finish == "raise":
                    raise RuntimeError(secret)
                return "late result"
            finally:
                loop.call_soon_threadsafe(exited.set)

        handle = exec_.submit("op", work)
        try:
            await asyncio.wait_for(entered.wait(), 5)
            cancelled = exec_.cancel(handle.job_id)
            exec_.clock.advance(60.0)  # type: ignore[attr-defined]
        finally:
            release.set()
        await asyncio.wait_for(exited.wait(), 5)
        # The follower can only run once the cancelled job's task has left
        # its slot, i.e. after the late outcome has been handled.
        follower = exec_.submit("follower", lambda: "ok")
        await _wait_until_settled(exec_, follower.job_id)

        assert exec_.status(handle.job_id) == cancelled
        outcome = exec_.result(handle.job_id)
        assert outcome.status == "cancelled"
        assert outcome.value is outcome.error is outcome.traceback is None

        records = [r for r in caplog.records if r.name == "msmcp.execution"]
        assert all(r.levelno < logging.WARNING for r in records)
        assert all(r.exc_info is None for r in records)
        assert all(secret not in r.getMessage() for r in records)
        discarded = [r for r in records if "Discarded late" in r.getMessage()]
        if finish == "raise":
            assert len(discarded) == 1
            assert discarded[0].levelno == logging.DEBUG
            assert "RuntimeError" in discarded[0].getMessage()
            assert handle.job_id in discarded[0].getMessage()
        else:
            assert discarded == []


# ---------------------------------------------------------------------------
# Retention
# ---------------------------------------------------------------------------
class TestRetention:
    async def test_completed_job_is_expired_after_its_ttl(self) -> None:
        exec_ = executor(default_ttl_seconds=None)
        handle = exec_.submit("op", lambda: 1, ttl_seconds=None)
        await _wait_until_settled(exec_, handle.job_id)

        # No TTL means the record is retained indefinitely.
        assert exec_.purge_expired() == 0
        assert exec_.status(handle.job_id).is_terminal

    async def test_sweep_drops_settled_jobs_past_their_deadline(self) -> None:
        exec_ = executor(default_ttl_seconds=60.0)
        handle = exec_.submit("op", lambda: 1)
        await _wait_until_settled(exec_, handle.job_id)

        clock = exec_.clock  # type: ignore[attr-defined]
        assert exec_.purge_expired() == 0
        clock.advance(61.0)
        assert exec_.purge_expired() == 1
        with pytest.raises(UnknownJobError):
            exec_.status(handle.job_id)

    async def test_sweep_never_drops_running_work(self) -> None:
        exec_ = executor(default_ttl_seconds=1.0)
        release = threading.Event()
        handle = exec_.submit("op", release.wait)
        clock = exec_.clock  # type: ignore[attr-defined]
        clock.advance(100.0)
        assert exec_.purge_expired() == 0
        assert not exec_.status(handle.job_id).is_terminal
        release.set()
        await _wait_until_settled(exec_, handle.job_id)

    async def test_ttl_is_driven_by_the_timer_as_well(self) -> None:
        """A finished job disappears from the store on its own after the TTL."""
        exec_ = executor()
        handle = exec_.submit("op", lambda: 1, ttl_seconds=0.05)
        await _wait_until_settled(exec_, handle.job_id)
        await asyncio.sleep(0.2)
        with pytest.raises(UnknownJobError):
            exec_.status(handle.job_id)


# ---------------------------------------------------------------------------
# Bounded concurrency
# ---------------------------------------------------------------------------
class TestConcurrencyCap:
    async def test_never_runs_more_than_max_concurrency_at_once(self) -> None:
        exec_ = executor(max_concurrency=2)
        gate = threading.Event()
        lock = threading.Lock()
        active = 0
        peak = 0

        def work() -> str:
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            gate.wait(5.0)
            with lock:
                active -= 1
            return "ok"

        handles = [exec_.submit("op", work) for _ in range(5)]
        # Give the scheduler a chance to start everything it can.
        for _ in range(50):
            await asyncio.sleep(0.01)
            if any(exec_.status(h.job_id).phase == "queued" for h in handles):
                break
        assert peak <= 2, f"peak concurrency {peak} exceeded the cap"

        # Queued jobs are visible as queued, not silently missing.
        assert any(exec_.status(h.job_id).phase == "queued" for h in handles)

        gate.set()
        for handle in handles:
            await _wait_until_settled(exec_, handle.job_id)
        assert all(exec_.result(h.job_id).status == "completed" for h in handles)

    async def test_list_jobs_reports_every_record(self) -> None:
        exec_ = executor()
        handles = [exec_.submit("op", lambda: 1) for _ in range(3)]
        for handle in handles:
            await _wait_until_settled(exec_, handle.job_id)
        listed = {snapshot.job_id for snapshot in exec_.list_jobs()}
        assert listed == {handle.job_id for handle in handles}


# ---------------------------------------------------------------------------
# Teardown
# ---------------------------------------------------------------------------
class TestTeardown:
    async def test_forget_drops_a_record(self) -> None:
        exec_ = executor()
        handle = exec_.submit("op", lambda: 1)
        await _wait_until_settled(exec_, handle.job_id)

        assert exec_.forget(handle.job_id) is True
        assert exec_.forget(handle.job_id) is False
        with pytest.raises(UnknownJobError):
            exec_.status(handle.job_id)

    async def test_shutdown_clears_everything(self) -> None:
        exec_ = executor()
        handle = exec_.submit("op", lambda: 1)
        await _wait_until_settled(exec_, handle.job_id)

        exec_.shutdown()
        assert len(exec_) == 0
        assert exec_.list_jobs() == ()
