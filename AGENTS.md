# MSMCP Development Instructions

## Project
Mass spectrometry data MCP server.

## Agent coordination
This repository is worked on by two agents at once, and this file is the shared
contract between them. Keep it accurate — both read it.

- **Zed agent** — the in-editor agent. Makes changes here, runs the local gate, and
  keeps its own conversation in Zed.
- **Hermes agent** — runs locally from this repository. It reads `AGENTS.md` from the
  **working directory only** (parent and subdirectory copies are ignored), so run it
  from the repo root for these rules to bind. It carries its own persistent memory and
  skills across sessions.

Work lanes — use the right one rather than duplicating:
- Independent review is the user-typed `/review` lane (`gpt-6-luna`); it snapshots the
  last ten messages and treats `skills/` as binding, so aim it at the end of a work
  block, before the diff scrolls out. Hermes' automatic background review only *adds*
  memory entries and is not a verification step.
- Multi-file or risky work gets a written plan first (`/plan` → `.hermes/plans/`).
- Hermes runs scheduled sweeps via cron with `workdir` set to the repo root, and does
  isolated edits in git worktrees; it never forks, opens PRs/issues, or publishes a
  release without explicit approval.
- `delegate_task` children are not a verification lane — they share the parent's blind
  spots.
- Coordinate before editing paths the other agent may be touching, and run the gate on
  the combined tree after any merge.

History: Hermes sessions live in `~/.hermes/state.db` (per-profile under
`~/.hermes/profiles/<name>/`); Zed conversations in
`~/Library/Application Support/Zed/threads/threads.db`. Neither agent has a verified
reader for the other's store, so pass context across explicitly.

Coordination notes for this project live in `~/Hermes/Projects/MSMCP/README.md`. To let
Hermes call this server's tools, register it under `mcp_servers` in
`~/.hermes/config.yaml` (`uv run --project <repo> msmcp`) and restart it.

## Python
- Python 3.12+
- pytest
- ruff
- basedpyright
- uv

## Engineering
- Prefer typed interfaces.
- Preserve public APIs unless explicitly changing them.
- Add tests for behavioural changes.
- Do not silently change mzML/MGF semantics.
- Do not invent mzML metadata.
- Validate external data at boundaries.

## Workflow
- Inspect existing implementation before editing.
- Run relevant tests after modifications.
- Run ruff and type checking before declaring a task complete.
- Keep changes focused.

## Isolated worktrees

Risky or parallel work happens in a linked worktree, never in a checkout that
holds someone else's uncommitted changes. Each worktree is an independent
checkout with its own environment.

- Create one per task, based on the integration branch:

  ```
  git worktree add .worktrees/<name> -b <branch> origin/main
  ```

- **Sync its environment before running any tool.** Each worktree gets its own
  `.venv`; without this step `uv run pytest` (and ruff/mypy) can fall back to the
  active environment from another checkout and import the wrong `msmcp`:

  ```
  uv sync --extra dev --offline
  ```

- Confirm the import resolves to this worktree, not another checkout:

  ```
  uv run python -c "import msmcp; print(msmcp.__file__)"
  ```

  It must print the worktree's own `src/msmcp/__init__.py`.

- **This is enforced, not just advised.** `tests/conftest.py` fails the whole
  run at import time if `msmcp`, `pytest`, or the running interpreter
  (`sys.prefix`) resolve outside this checkout's `.venv`. A test run that
  exercises one revision while importing another is a reproducibility hazard, so
  it is a hard error with an actionable message rather than a silent pass. The
  three must agree:

  ```
  uv run python -c "import msmcp, pytest, sys; print(msmcp.__file__); print(pytest.__file__); print(sys.prefix)"
  ```

  `sys.prefix` (not `sys.executable`) is checked: uv symlinks
  `.venv/bin/python` to its managed interpreter, so `sys.executable` can report
  the base path even when the venv is active.

- If the shell exports `VIRTUAL_ENV` from the primary checkout, `uv run` reports
  that it "does not match the project environment path" and ignores it — that is
  expected. `unset VIRTUAL_ENV` if a tool still resolves to the wrong checkout.

- Remove when done: `git worktree remove .worktrees/<name>`, then delete the
  branch. `.worktrees/` is gitignored.

These two commands are the whole procedure. A `make` target was considered and
deliberately not added: the real failure mode is environment resolution, not
worktree creation, so the sync step should stay visible rather than be hidden
behind a target.

## Gate and pushing

- Gate: `make lint` -> `make test` -> `make eval`, mirrored by `.github/workflows/ci.yml`.
  `make test` is the fast unit loop; `make eval` runs the evaluation notebook end to end and is
  the slow one. `make all` chains the whole pipeline, and `make format` is the one that writes.
  Full detail is in `README.md`.
- If `git push` fails with `could not read Username`, the credential helper is not wired up for a
  non-interactive shell. Supply it explicitly for that one command:

  ```
  git -c credential.helper= -c credential.helper='!gh auth git-credential' push origin <branch>
  ```

## Skills
- `skills/` holds agent playbooks for this project, version-controlled with the code.
- See `skills/README.md` for the layout and the conventions.
