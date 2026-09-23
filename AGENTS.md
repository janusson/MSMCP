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