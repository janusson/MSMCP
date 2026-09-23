"""End-to-end evaluation of ``notebooks/eval_msmcp.ipynb``.

The evaluation notebook is MSMCP's end-to-end suite: it calls the real
registered tool callables, drives the real in-process async job store,
exercises the real embedding adapters, and writes real mzML fixtures under
``notebooks/.eval_artifacts/``.  This module has two halves:

* :func:`test_notebook_is_structurally_valid` — fast; runs with the normal unit
  suite and guards the notebook file itself (valid JSON, compiles, no stored
  outputs).
* :func:`test_eval_notebook_passes_every_check` — marked ``eval``; executes the
  whole notebook headlessly and fails if any recorded check reports FAIL.

Run the slow half with::

    make eval
    # or, equivalently
    uv run pytest tests/test_eval_notebook.py -m eval -s -q

The notebook deliberately writes its fixtures *inside* ``notebooks/`` because it
asserts MSMCP's default filesystem boundary, which is resolved from the working
directory at import time — so invoke pytest from the repository root.
"""

from __future__ import annotations

import contextlib
import io
import json
import logging
import os
import time
import traceback
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
NOTEBOOK_PATH = REPO_ROOT / "notebooks" / "eval_msmcp.ipynb"
ARTIFACT_DIR = NOTEBOOK_PATH.parent / ".eval_artifacts"
REPORT_PATH = ARTIFACT_DIR / "eval_report.json"

#: Guard against a notebook that silently stops evaluating anything.
MIN_EXPECTED_CHECKS = 60


class NotebookCellError(RuntimeError):
    """A notebook cell raised; the message carries the cell source."""


def _load_notebook() -> dict[str, Any]:
    """Parse the notebook, asserting the basics of the nbformat container."""
    notebook: dict[str, Any] = json.loads(NOTEBOOK_PATH.read_text(encoding="utf-8"))
    assert notebook["nbformat"] == 4, "expected nbformat 4"
    assert notebook["cells"], "notebook contains no cells"
    return notebook


def _code_cells(notebook: dict[str, Any]) -> list[str]:
    """Return the source of every code cell, in order."""
    return [
        "".join(cell["source"])
        for cell in notebook["cells"]
        if cell["cell_type"] == "code"
    ]


def _registered_tool_names() -> set[str]:
    """Every tool name the server actually publishes, from the source tree."""
    from msmcp.tools import chem, io, qc, search, similarity

    captured: set[str] = set()

    class _Capture:
        def tool(
            self, **_metadata: Any
        ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
            def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
                captured.add(fn.__name__)
                return fn

            return decorator

    for module in (chem, io, qc, search, similarity):
        module.register_tools(_Capture())
    return captured


def _execute_notebook() -> tuple[dict[str, Any], str, float]:
    """Execute every code cell in one namespace.

    Returns the final namespace, the captured stdout/stderr, and the wall clock
    in milliseconds.  Raises :class:`NotebookCellError` if a cell raises.
    """
    notebook = _load_notebook()
    namespace: dict[str, Any] = {"__name__": "__main__"}
    log = io.StringIO()
    started = time.perf_counter()
    with contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
        for index, cell in enumerate(notebook["cells"]):
            if cell["cell_type"] != "code":
                continue
            source = "".join(cell["source"])
            try:
                exec(
                    compile(source, f"{NOTEBOOK_PATH.name}:cell[{index}]", "exec"),
                    namespace,
                )
            except Exception as exc:
                raise NotebookCellError(
                    f"notebook cell {index} raised {type(exc).__name__}: {exc}\n"
                    f"--- cell source ---\n{source}\n"
                    f"--- traceback ---\n{traceback.format_exc()}"
                ) from exc
    return namespace, log.getvalue(), (time.perf_counter() - started) * 1000.0


def _shutdown(namespace: dict[str, Any], rollout: list[Any]) -> None:
    """Stop the notebook's private event loop and restore process state.

    The notebook keeps its event loop running so individual cells stay
    re-runnable; the headless runner owns the shutdown instead.  Logging is
    restored because the notebook reconfigures the root logger with
    ``force=True`` to keep its output on stderr.
    """
    runner = namespace.get("RUNNER")
    loop = getattr(runner, "_loop", None)
    if loop is not None and loop.is_running():
        loop.call_soon_threadsafe(loop.stop)
        thread = getattr(runner, "_thread", None)
        if thread is not None:
            thread.join(timeout=10.0)

    root = logging.getLogger()
    root.handlers[:] = rollout[0]
    root.setLevel(rollout[1])
    os.environ["MSMCP_EMBEDDING_BACKEND"] = rollout[2]


def _summarise(results: list[Any], elapsed_ms: float) -> str:
    """Render a compact per-tool report for the terminal."""
    by_tool: dict[str, list[Any]] = {}
    for row in results:
        by_tool.setdefault(row.tool, []).append(row)

    header = (
        f"{'tool':<24} {'cases':>5} {'pass':>5} {'fail':>5} {'skip':>5} "
        f"{'total_ms':>10} {'max_ms':>9}"
    )
    lines = [header, "-" * len(header)]
    for tool in sorted(by_tool):
        rows = by_tool[tool]
        runtimes = [row.runtime_ms for row in rows]
        lines.append(
            f"{tool:<24} {len(rows):>5} "
            f"{sum(r.status == 'PASS' for r in rows):>5} "
            f"{sum(r.status == 'FAIL' for r in rows):>5} "
            f"{sum(r.status == 'SKIP' for r in rows):>5} "
            f"{sum(runtimes):>10.1f} {max(runtimes):>9.1f}"
        )
    lines.append("-" * len(header))
    totals = {
        status: sum(r.status == status for r in results)
        for status in ("PASS", "FAIL", "SKIP")
    }
    lines.append(
        f"{'TOTAL':<24} {len(results):>5} {totals['PASS']:>5} "
        f"{totals['FAIL']:>5} {totals['SKIP']:>5}"
    )
    lines.append(
        f"verdict: {'FAILED' if totals['FAIL'] else 'PASSED'} | "
        f"notebook wall clock {elapsed_ms / 1000:.2f} s | report: {REPORT_PATH}"
    )
    skipped = [row for row in results if row.status == "SKIP"]
    for row in skipped:
        lines.append(f"  skipped: [{row.tool}] {row.case} — {row.schema_assertion}")
    return "\n".join(lines)


def _write_report(results: list[Any], elapsed_ms: float) -> None:
    """Persist the machine-readable version of the run (fixtures live alongside)."""
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(
        json.dumps(
            {
                "notebook": NOTEBOOK_PATH.name,
                "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
                "elapsed_ms": round(elapsed_ms, 1),
                "totals": {
                    status.lower(): sum(row.status == status for row in results)
                    for status in ("PASS", "FAIL", "SKIP")
                },
                "checks": [
                    {
                        "tool": row.tool,
                        "case": row.case,
                        "input_status": row.input_status,
                        "status": row.status,
                        "runtime_ms": round(row.runtime_ms, 3),
                        "schema_assertion": row.schema_assertion,
                    }
                    for row in results
                ],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Fast structural guard — part of the default unit run
# ---------------------------------------------------------------------------
def test_notebook_is_structurally_valid() -> None:
    """The notebook parses, compiles, and carries no stored outputs."""
    notebook = _load_notebook()
    assert NOTEBOOK_PATH.stat().st_size > 0, "notebook is empty"

    code_cells = 0
    for index, cell in enumerate(notebook["cells"]):
        assert cell["cell_type"] in ("code", "markdown"), f"cell {index}: bad cell_type"
        assert cell["source"], f"cell {index}: empty source"
        if cell["cell_type"] != "code":
            continue
        code_cells += 1
        assert cell.get("execution_count") is None, (
            f"cell {index}: stored execution count"
        )
        assert cell.get("outputs") == [], f"cell {index}: stored outputs"
        compile("".join(cell["source"]), f"{NOTEBOOK_PATH.name}:cell[{index}]", "exec")

    assert code_cells >= 20, (
        f"only {code_cells} code cells found; notebook looks truncated"
    )


# ---------------------------------------------------------------------------
# Slow end-to-end run — `make eval`
# ---------------------------------------------------------------------------
@pytest.mark.eval
def test_eval_notebook_passes_every_check() -> None:
    """Execute the evaluation notebook and require zero failing checks."""
    from msmcp.security import DEFAULT_POLICY

    # The notebook asserts the filesystem boundary against the fixtures it
    # writes inside notebooks/; from any other directory that guard would fail
    # for the wrong reason.
    if not ARTIFACT_DIR.is_relative_to(DEFAULT_POLICY.allowed_root):
        pytest.skip(
            f"MSMCP's security root is {DEFAULT_POLICY.allowed_root}; run pytest "
            f"from {REPO_ROOT} so the notebook's fixtures stay inside it"
        )

    root = logging.getLogger()
    rollout = [
        root.handlers[:],
        root.level,
        os.environ.get("MSMCP_EMBEDDING_BACKEND", ""),
    ]
    namespace: dict[str, Any] = {}
    try:
        namespace, log, elapsed_ms = _execute_notebook()
    finally:
        _shutdown(namespace, rollout)

    results = namespace["RESULTS"]
    failures = [row for row in results if row.status == "FAIL"]
    _write_report(results, elapsed_ms)
    print("\n" + _summarise(results, elapsed_ms))

    if failures:

        def _excerpt(row: Any) -> str:
            detail = row.detail or "none"
            # A traceback is more useful from the end (the exception message);
            # a predicate failure is more useful from the start.
            return "…" + detail[-500:] if "Traceback" in detail else detail[:500]

        detail = "\n\n".join(
            f"[{row.tool}] {row.case} :: {row.schema_assertion}\n"
            f"  input: {row.input_status}\n"
            f"  detail: {_excerpt(row)}"
            for row in failures
        )
        pytest.fail(
            f"{len(failures)} of {len(results)} evaluation checks FAILED\n\n"
            f"{detail}\n\n--- notebook log (tail) ---\n{log[-4000:]}",
            pytrace=False,
        )

    covered = {row.tool for row in results}
    missing = _registered_tool_names() - covered
    assert not missing, f"the notebook never exercised: {sorted(missing)}"
    assert len(results) >= MIN_EXPECTED_CHECKS, (
        f"only {len(results)} checks recorded (expected >= {MIN_EXPECTED_CHECKS}); "
        "the notebook may have silently stopped evaluating"
    )
