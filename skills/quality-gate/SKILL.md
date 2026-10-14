---
name: quality-gate
description: The format, lint, test, and eval commands to run before declaring any msmcp task complete, and the conventions they enforce.
category: workflow
version: 1.0.0
---

# Quality gate

Run this before declaring any task complete. All Python targets go through
`uv`; the project declares Python 3.13 in `pyproject.toml`.

## Commands

| Purpose | Command |
|---|---|
| Sync deps | `make install` (`uv sync --extra dev`) |
| Auto-format | `make format` (`ruff format .` then `ruff check --fix .`) |
| Full static gate | `make lint` |
| Unit tests | `make test` |
| Notebook eval | `make eval` |
| Scientific validation (opt-in) | `make validate-similarity` (needs an external dataset + `matchms`) |
| Everything | `make all` |

`make lint` runs, in order: `ruff format --check .`, `ruff check .`, `uv run
mypy`, `uv run basedpyright`. Run it exactly as CI does rather than invoking
the tools individually, so a green local run means a green CI run.

## What the gate enforces

- **ruff**: line length 88, target `py313`, rules `E4, E7, E9, F, I, UP, B, SIM,
  RUF`. Auto-fixable findings are handled by `make format`.
- **mypy** (`src` and `tests`, `mypy_path = ["src"]`) and **basedpyright**
  (`typeCheckingMode = "standard"`). The project holds zero warnings as debt:
  `src/`, `tests/`, and the eval notebook all check clean. Do not silence a
  finding to get green; fix the cause.
- **pytest**: `testpaths = ["tests"]`, with `-m 'not eval'` so `make test`
  stays a fast unit run. The `eval` marker is the notebook suite.

## Test surface

- Tool tests exercise raw callables captured by the `FakeMCP` fixtures in
  `tests/conftest.py`.
- `tests/test_tool_schemas.py` asserts the wire contract from `tools/list`.
- `tests/test_smoke_stdio.py` launches the real server as a subprocess and
  drives a JSON-RPC session; it is the guard for the "never write to stdout"
  rule and for the exact registered tool set.
- `tests/test_eval_notebook.py` runs `notebooks/eval_msmcp.ipynb` under the
  `eval` marker.
- `tests/test_spearman.py` guards the tie-aware correlation reported by the
  similarity validation (it runs in the fast suite; no optional dependency).
- `tests/test_validation_harness.py` covers the shipped validation harness's
  protocol — the query/database split, the leakage guard, and the
  absent-versus-invalid dataset behaviour — with neither `matchms` nor the
  dataset.
- `validation/` holds the opt-in scientific-validation harness. It is **not**
  collected by pytest and **not** part of the gate; `matchms` is supplied only
  for `make validate-similarity` and is never a runtime dependency.

## Conventions the gate assumes

- Prefer typed interfaces; every public callable is annotated.
- Preserve public APIs unless the task explicitly changes them.
- Add tests for behavioural changes.
- Keep changes focused; do not fix unrelated failures.
- `drafts/` is gitignored scratch and excluded from lint and type checking;
  nothing shipped may live there.
- `validation/` is shipped and type-checked (it is in `mypy`'s `files`); it must
  import cleanly **without** `matchms`, so the fast gate passes on a machine that
  never installs the optional validation dependency.

## Checklist

- [ ] `make test` passes (or the specific new test does).
- [ ] `make lint` passes with no new suppressions.
- [ ] Behavioural changes have tests.
- [ ] If the notebook changed, `make eval` passes.
