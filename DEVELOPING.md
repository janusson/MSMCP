# Developing MSMCP

This document holds the maintainer-facing detail — internals, testing, tooling
and layout — kept out of the [README](README.md) so the README can stay the
user- and agent-facing story (*what it does and how to use it*). For scope,
layer rules and the acceptance criteria, see
[ARCHITECTURE.md](ARCHITECTURE.md); for the plan of record, see
[docs/roadmap.md](docs/roadmap.md).

## Spectral foundation models

`src/msmcp/models/embeddings.py` defines a single adapter contract:

```python
class SpectralEmbedder(ABC):
    name: ClassVar[str]
    backend: ClassVar[str] = "mock"  # "mock" (test/dev-only) | "hf" (real inference)
    embedding_dim: int = 1024

    @staticmethod
    @abstractmethod
    def check_available() -> None: ...  # raises when the backend is unusable

    @abstractmethod
    def embed_spectrum(self, peaks, precursor_mz=None) -> np.ndarray: ...
```

- **Deterministic mocks (test/dev-only)** — `DreaMSEmbedder` and `LSMMS2Embedder` project peaks onto a fixed 1024-bin m/z grid, apply per-model intensity compression (√ for DreaMS, log1p for LSM-MS2), seed deterministic noise from the precursor m/z + peak content (order-independent BLAKE2b hash), and L2-normalise to `float32`. Identical spectra score exactly 1.0; shared peaks score proportionally; disjoint spectra score 0.0. **These are not learned models**: they exist only for hermetic tests/development, are reachable only under `MSMCP_EMBEDDING_BACKEND=mock`, and must never be reported as scientific results.
- **Real inference (DreaMS)** — `DreaMSInferenceEmbedder` (`src/msmcp/models/backends.py`) runs the official pre-trained 1024-d transformer (Bushuiev et al., *Nature Biotechnology* 2025). Install the package from source (`uv pip install "git+https://github.com/pluskal-lab/DreaMS.git"` — the `dreams` name on PyPI is an unrelated nanophotonics library) and the embedding checkpoint downloads automatically on first use. Spectra are embedded through the model's own preprocessing pipeline (DataFormat-A: peaks sorted by m/z, intensity max-normalised, fragments strictly below the precursor) via a temporary MGF file; the checkpoint load is cached per process and the output is L2-normalised float32.
- **Real inference (LSM-MS2)** — no public inference weights exist upstream (only peer-review code, `matterworksbio/LSM1-MS2`), so `LSMMS2InferenceEmbedder` is a bring-your-own-checkpoint adapter activated by the `MSMCP_LSM_MS2_CKPT` environment variable (the checkpoint must expose `encode(mz, intensity, precursor_mz)`); the embedding dimensionality is derived from the model output.
- **Backend selection** — `MSMCP_EMBEDDING_BACKEND=real|mock` (default `real`): `real` requires real inference and raises `EmbeddingBackendUnavailable` with install instructions when a model/checkpoint is unavailable; `mock` explicitly opts into the deterministic mocks for tests/development only. Legacy `auto`/`hf` values are treated as `real`. Tool reports disclose which backend produced each score (`real inference` vs `mock (dev/test-only, not a learned model)`).

## Asynchronous job orchestration

All long-running work goes through one interface, `JobExecutor` (`src/msmcp/execution/executor.py`):

```python
class JobExecutor(ABC):
    def submit(
        self, operation, fn, *, parameters=None, ttl_seconds=None
    ) -> JobHandle: ...
    def status(self, job_id) -> JobStatusSnapshot: ...
    def result(self, job_id) -> JobResult: ...
    def cancel(self, job_id) -> JobStatusSnapshot: ...
    def forget(self, job_id) -> bool: ...
    def list_jobs(self) -> tuple[JobStatusSnapshot, ...]: ...
    def shutdown(self) -> None: ...
```

v1.0 ships `LocalAsyncExecutor`: CPU-bound work dispatched with `asyncio.to_thread`, bounded by a semaphore (`max_concurrency`, default 4), with a one-hour TTL on finished records. No external service, daemon or database is involved.

- The dispatcher (`search_library`) resolves the real query peaks, then `submit`s the scan and returns a job ID immediately.
- The poller (`check_search_status`) reads the snapshot: `queued`/`running` → a wait message, `completed` → the full report **once** and a short digest on every later poll (`full_report=True` re-requests the report itself), `failed` → the exception traceback, `cancelled` → confirmation that partial work was discarded.
- A job the executor no longer knows about (a restart, or after its TTL) is reported as a **terminal failure**, never as an ambiguous pending state a client could spin on.

**Statuses are MCP task statuses** (`working` / `completed` / `failed` / `cancelled`), and `JobStatusSnapshot.to_task_dict()` renders a snapshot into the shape of an MCP `Task`. The installed SDK (`mcp` 2.1.1) exposes generic extension request registration and capability advertisement, but does not provide MSMCP with a Tasks-specific adapter. MSMCP exposes job semantics through its own polling tool and keeps the wire representation aligned with the MCP `Task` shape; implementing the Tasks extension remains roadmap issue #7.

The readiness check exercises those extension hooks rather than inspecting core request unions, because Tasks are intentionally an extension:
`tests/test_server_contract.py::test_installed_sdk_can_register_and_advertise_extension_requests`. Run it with:

```bash
uv run pytest tests/test_server_contract.py -q
```

If that test fails after an SDK upgrade, revisit the extension adapter plan for
#7 before upgrading the release. Passing confirms that the generic hooks remain
available; it does not mean MSMCP has implemented the Tasks extension.

**Cancellation is honest about its limits:** a worker thread cannot be interrupted, so cancelling marks the job terminal and *discards* its output. A caller that sees `cancelled` never sees a partial result.

**Portability:** the executor is the seam for a process pool, a job queue or Prefect. Nothing in v1.0 depends on one, and `JobExecutor` is what makes adding one a contained change.

## Distribution checks

The root `Dockerfile` is intentionally a local packaging starting point, not a
published artifact. It uses `uv sync --frozen` against `uv.lock` and launches
the same stdio entry point as a source checkout. Build it locally with:

```bash
docker build -t msmcp:local .
```

Registry metadata, package publication, image signing, and an offline install
procedure remain open parts of issue #19.

## Developer ergonomics

- **Testing**: over 500 pytest cases across `tests/` (the `eval`-marked notebook test is deselected in the default `make test` run) — chemistry (exact masses against literature values, adduct validation), similarity (5.0-ppm boundary arithmetic, greedy matching, embedding semantics), **spectral scoring** (`test_scoring.py`: the `SpectrumScorer` contract and its invariants — unmatched intensity must lower the score, empty/zero-intensity spectra score 0.0 not `nan`, scorers are pure and thread-safe), embedding backends (backend resolution, hermetic real-inference pipelines with stubbed models, checkpoint-load safety gating, stdio transport protection), the **data-reference store** (`test_pointers.py`: registration, immutability, malformed/unknown/expired/wrong-kind failures, namespace isolation, byte and count ceilings, concurrent access from real threads), **provenance** (`test_provenance.py`: immutability, JSON-safety, digests, the multi-step chain), the **job executor** (`test_executor.py`: submit/status/result/cancel, non-blocking dispatch, concurrency cap, TTL sweep, traceback-carrying failures), **ingestion** (`test_mgf.py`, `test_ingest.py`: MGF parsing and its malformed inputs, format dispatch, MassFlow-backed imzML including the stdout-logging guard and a corrupt-payload path), search (a full dispatcher → executor → poller round trip, the once-then-digest delivery contract and its re-request, real vs. synthetic halves of the pipeline, scorer routing, cancellation, failure), the **spectral library reader** (`test_library.py`: a real MSP library searched end to end, the security-boundary refusal, the pre-dispatch probe that makes a malformed library an immediate tool error, and an empty library that is read and reports no hits; `test_library_benchmark.py`: ground-truth per-perturbation recovery, including a perturbation that must fail to recover), the **spectrum index and ion mode** (`test_io.py`: the `list_spectra` index tool — MS-level and minimum-peak filters, the `max_spectra` truncation and the security-boundary refusal; plus the polarity each reader reports and the search report's polarity-mismatch flag, in `test_mzml.py`, `test_mgf.py` and `test_library.py`), the **exact isotope envelope** (`test_chem.py`: the multinomial M…M+n expansion, the previously-truncated halogen M+4 case, and the `isotopologues` bound), mzML parsing, QC, the security boundary, the **wire contract** (`test_tool_schemas.py`), a **server-contract guard** (`test_server_contract.py`: the installed SDK still omits the `tasks/get`, `tasks/result` and `tasks/cancel` request methods MSMCP's own polling tool substitutes for), an **end-to-end workflow** (`test_workflow.py`: load → reference → summarise → search → provenance → release, plus the assertion that a 100k-peak spectrum serialises to under 4 kB), and an **end-to-end stdio smoke test** (`test_smoke_stdio.py`, the only test that drives the real transport: initialize handshake, `tools/list`, a `tools/call`, and a check that every stdout line parses as JSON-RPC), and the **tie-aware correlation** the similarity validation reports (`test_spearman.py`). Tests run hermetically: embedding backends are pinned to the test/dev-only deterministic mocks and no network or external services are required.
- **End-to-end evaluation**: `notebooks/eval_msmcp.ipynb` is the benchmark/stress suite — precursor and adduct boundaries, embedding shape/determinism/degenerate-input cases, real mzML parsing and QC, the full async search state machine (including failure, cancellation, lost-job and concurrency-cap behaviour), the server-side reference flow, and a per-tool diagnostic table. `tests/test_eval_notebook.py::test_eval_notebook_passes_every_check` executes every code cell headlessly and fails if any recorded check fails or if a registered tool is never exercised; `make eval` runs it and prints the per-tool roll-up (a machine-readable copy lands in `notebooks/.eval_artifacts/eval_report.json`). It is marked `eval` and excluded from the default `pytest` run so `make test` stays fast — `make all` runs both. A structural guard (`test_notebook_is_structurally_valid`) runs with the normal suite and fails if the notebook stops parsing, compiles, or has stored outputs.
- **Scientific validation (opt-in)**: `validation/similarity_validation.py` compares MSMCP's `ClassicalScorer` against `matchms` `CosineGreedy` on a pinned MassBank release and measures held-out library recovery; `make validate-similarity` runs it, supplying `matchms` for that one command so neither the runtime nor `make test` depends on it (it skips cleanly when the dataset is absent). The claims, tolerances and per-capability status live in [`SCIENTIFIC_VALIDATION.md`](SCIENTIFIC_VALIDATION.md); the experiment and its stated limitations in [`VALIDATION_SIMILARITY.md`](VALIDATION_SIMILARITY.md). It is not part of the gate.
- **Where parameter documentation lives**: the MCP SDK builds each tool's `inputSchema` from the **function signature**, not from Pydantic models. Descriptions therefore belong on `Annotated[..., Field(description=...)]` in the signature; a description added only to an input model never reaches the host. The Pydantic models in each `tools/` module are for *validation* only (they are what raises `ValidationError` on bad arguments), and they deliberately carry no `description=`. `tests/test_tool_schemas.py` fails the build if a parameter reaches the wire undocumented, or if a body constraint (e.g. `ge=1, le=50`) is missing from the schema.
- **The server instructions are part of the wire contract too**: `msmcp.server`'s `instructions` string is the first thing a host reads, so it must agree with the tool descriptions. `tests/test_tool_schemas.py` asserts that it does — the drift that once told a model `search_library` was *always* synthetic is now a failing test.
- **Linting/typing**: `ruff` (E/F/I/UP/B/SIM/RUF), `mypy` and `basedpyright` all run clean over `src/`, `tests/` and the evaluation notebook, via `uv run ruff check .`, `uv run mypy` and `uv run basedpyright`. There are **no per-file suppressions for source code and no `ignore_errors` overrides**: the temporary debt from the earlier milestones has been removed rather than relocated. The only remaining ignore is `E402` on the notebook, where a cell must set environment flags before importing `msmcp`. The gitignored `drafts/` scratch directory is excluded from all three — it holds one-off scripts and audit probes, not shipped code; the audit *report* itself is kept in `docs/audit/`. The shipped `validation/` harness **is** type-checked (`mypy`'s `files` includes it) and must import cleanly without `matchms`, so the gate passes on a machine that never installs the optional validation dependency.
- **Formatting**: `ruff format`, line length 88, PEP 695 syntax, `from __future__ import annotations` throughout.
- **Design log**: [`docs/mcp-server-configuration.md`](docs/mcp-server-configuration.md) is an unedited transcript of the early host-configuration sessions — useful context for *why* the transport and logging boundaries are what they are, not a reference manual.
- **Audit**: [`docs/audit/2026-09-22-standard-audit.md`](docs/audit/2026-09-22-standard-audit.md) is a dated, unedited audit of the v1.0 tree. The four critical findings it reports were repaired the same day (`eeeed69`, `44ac6b7`, `b77d5fe`), and its ordered backlog is where the open work items come from. It describes the tree it was written against, not the current one.

## Repository layout

```text
msmcp/
├── .github/workflows/ci.yml     # CI: the same gate `make lint`/`test`/`eval` run locally
├── pyproject.toml               # deps, dev extras, ruff/mypy/pytest config
├── uv.lock                      # reproducible lockfile
├── Makefile                     # developer pipeline: install/format/lint/test/eval/all (+ opt-in validate-similarity)
├── README.md                    # project overview: what it does, how to use it
├── DEVELOPING.md                # this file: internals, testing, tooling, layout
├── ARCHITECTURE.md              # scope, layers, limitations, acceptance criteria
├── CHANGELOG.md                 # what landed, by release
├── SCIENTIFIC_VALIDATION.md     # per-capability claim / evidence / status
├── VALIDATION_SIMILARITY.md     # the matchms/MassBank similarity experiment and its limits
├── docs/
│   ├── roadmap.md               # the maintained plan of record
│   ├── mcp-server-configuration.md  # unedited design-session transcript (host setup)
│   └── audit/
│       └── 2026-09-22-standard-audit.md  # dated audit: findings and ordered backlog
├── examples/
│   ├── caffeine_library.msp     # synthetic worked-example MSP/NIST library (5 compounds)
│   ├── caffeine_spectrum.mgf    # synthetic worked-example MS2 query spectrum
│   ├── msmcp_infographic.html   # one-page visual overview (illustrative)
│   └── msmcp_interactive_application.html  # interactive mock-up (illustrative)
├── src/msmcp/
│   ├── server.py                # MCPServer transport layer + entry point
│   ├── errors.py                # shared error taxonomy (malformed/missing/inaccessible)
│   ├── security.py              # local file-access boundary (root, size, spectrum limits)
│   ├── provenance.py            # structured, immutable provenance records
│   ├── ingest.py                # format dispatch: file -> the right reader
│   ├── mzml.py                  # dependency-free mzML reader (MSMCP-owned)
│   ├── mgf.py                   # dependency-free MGF reader (MSMCP-owned)
│   ├── massflow_io.py           # MassFlow-backed imzML reader + stdout-logging guard
│   ├── library.py               # LibraryProvider + MSP/NIST text and MGF library readers
│   ├── instruments.py           # instrument-class defaults for every experimental threshold
│   ├── state/
│   │   ├── pointers.py          # DataReference + PointerStore (bounded, TTL, namespaces)
│   │   └── store.py             # the process-wide store and tool-facing helpers
│   ├── execution/
│   │   └── executor.py          # JobExecutor interface + LocalAsyncExecutor
│   ├── models/
│   │   ├── embeddings.py        # SpectralEmbedder ABC + test/dev-only deterministic mocks
│   │   ├── scoring.py           # SpectrumScorer contract + classical/embedding scorers
│   │   ├── null_model.py        # fitted upper-tail null for search significance (#12)
│   │   └── backends.py          # real-inference adapters (DreaMS/LSM-MS2) + resolver
│   └── tools/
│       ├── io.py                # ingestion, data references, compact summaries
│       ├── chem.py              # adduct shifts, isotope annotation
│       ├── similarity.py        # ppm validation, classical + embedding cosine
│       ├── qc.py                # truthful QC metrics
│       ├── search.py            # executor-backed library scan
│       └── system.py            # server-level diagnostic tools (ping)
├── validation/
│   └── similarity_validation.py # opt-in: MSMCP vs matchms on a pinned MassBank release
├── notebooks/
│   ├── eval_msmcp.ipynb         # end-to-end evaluation/benchmark suite (see `make eval`)
│   └── .eval_artifacts/         # generated mzML fixtures + eval_report.json (gitignored)
└── tests/
    ├── conftest.py              # hermetic fixtures (mzML, MGF, imzML) + tool harness
    ├── test_chem.py
    ├── test_similarity.py
    ├── test_embeddings.py
    ├── test_scoring.py          # SpectrumScorer contract + its scientific invariants
    ├── test_search.py
    ├── test_security.py
    ├── test_mzml.py
    ├── test_mgf.py
    ├── test_ingest.py           # format dispatch + MassFlow imzML integration
    ├── test_instruments.py      # instrument-class defaults and their documented provenance
    ├── test_io.py               # list_spectra, the reference flow, and the scan budget
    ├── test_qc.py
    ├── test_pointers.py         # the data-reference store
    ├── test_provenance.py
    ├── test_executor.py         # the JobExecutor contract
    ├── test_library.py          # real library reader + the security boundary it crosses
    ├── test_library_benchmark.py # ground-truth per-perturbation recovery
    ├── test_null_model.py       # fitted upper-tail null, validated against sampled p-values
    ├── test_quickstart_example.py # the README's "Try it in 60 seconds", run as written
    ├── test_workflow.py         # end-to-end: load -> reference -> search -> provenance
    ├── test_tool_schemas.py     # wire contract: titles, annotations, param docs, constraint drift
    ├── test_server_contract.py  # server instructions + the installed-SDK Tasks assumption
    ├── test_zed_config.py       # .zed/settings.json and the README launch snippet agree
    ├── test_eval_notebook.py    # runs the eval notebook headlessly (`make eval`)
    ├── test_spearman.py         # tie-aware Spearman used by the similarity validation
    ├── test_validation_harness.py # validation harness protocol: split, leakage guard, skip/fail
    └── test_smoke_stdio.py      # end-to-end JSON-RPC session over a real stdio pipe
```
