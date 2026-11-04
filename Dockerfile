FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim@sha256:531f855bda2c73cd6ef67d56b733b357cea384185b3022bd09f05e002cd144ca

WORKDIR /app

# Keep dependency resolution reproducible from the committed lockfile.
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev

# MSMCP is a local MCP server: JSON-RPC uses stdio, not a listening port.
ENTRYPOINT ["uv", "run", "--frozen", "--no-dev", "msmcp"]
