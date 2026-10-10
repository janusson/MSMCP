# MSMCP Development Instructions

## Project
Mass spectrometry data MCP server.

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
