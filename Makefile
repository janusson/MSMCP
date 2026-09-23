# MSMCP — developer ergonomics
# =============================================================================
# Python targets run through `uv`, so the only prerequisite for those is a
# working `uv` (https://docs.astral.sh/uv/).  Python itself is managed via
# .python-version.  The `repomix` target additionally requires the `repomix`
# CLI (https://repomix.com/, Node-based).

.PHONY: install format lint test eval run repomix all

install:            ## Create/update the virtualenv and sync all deps (incl. dev extras)
	uv sync --extra dev

format: install     ## Auto-format code and apply safe fixes (ruff)
	uv run ruff format .
	uv run ruff check --fix .

lint: install       ## Static checks: ruff + mypy + basedpyright
	uv run ruff check .
	uv run mypy
	uv run basedpyright

test: install       ## Run the fast pytest suite (the eval notebook is deselected)
	uv run pytest

eval: install       ## Execute notebooks/eval_msmcp.ipynb end to end and report
	uv run pytest tests/test_eval_notebook.py -m eval -s -q

run:                ## Launch the MCP server on the stdio transport
	uv run msmcp

repomix:            ## Pack the repo into repomix-output.xml for AI analysis
	repomix --style xml --output repomix-output.xml

all: install format lint test eval repomix ## Full pipeline: sync, format, lint, test, evaluate, repomix-pack
