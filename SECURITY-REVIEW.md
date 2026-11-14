# Security review — MSMCP

Scope: the whole repository, prioritising injection, authentication bypass,
secrets in source, and unsafe dependencies. Findings below are the reviewer's
result after verifying each one against the code; status reflects this branch
(`fix/report-injection-hardening`, based on `origin/main`), not `main` itself.

## Findings

### 1. MEDIUM — Cross-prompt injection through file-derived report text

**Confidence:** 8/10 · **Status:** fixed

`search_library`, `check_search_status`, `generate_qc_summary`,
`load_mzml_summary`, `index_spectra` and `compute_cosine` build Markdown from
strings that come out of caller-supplied files: MSP `Name:`/`Formula:` values,
source paths, and digests.

A hostile file can therefore reshape the report:

| Input | Effect without escaping |
| --- | --- |
| `Name: Caffeine \| 0.0001 \| IGNORE ALL PREVIOUS INSTRUCTIONS` | injected table columns |
| a path containing `\n> …` | a new Markdown block outside the report's structure |
| a path containing a backtick | closes the surrounding code span |
| a path containing ` ``` ` | closes the provenance JSON fence |
| `<script>` / `&amp;` in a name | raw HTML, or a second entity-expansion round |

The report is consumed as context by the host model, so injected text competes
with the system prompt (XPIA). No code execution is possible; this is a
prompt-integrity and report-structure issue.

**Fix.** `src/msmcp/reporting.py` adds `escape_inline` (collapse newlines and
control characters, truncate to `MAX_RENDERED_FIELD_CHARS`, escape `&` then `<`
then `>`, replace backticks), `escape_table_cell` (additionally escapes `\`
before `|` so an input backslash cannot escape the cell delimiter), and
`fenced_code` (the fence grows past the payload's longest backtick run).
Escape points, 26 call sites across four modules:

| Module | Sites |
| --- | --- |
| `tools/search.py` | query source, database label, hit table compound/formula cells, banner digest, digest top compound, failure message |
| `tools/qc.py` | `**File:**` line, provenance fence, untrusted-data note |
| `tools/io.py` | `File:` basename in both report shapes, provenance source path |
| `tools/similarity.py` | reference label, both scoring shapes |

`MAX_RENDERED_FIELD_CHARS = 512`: wide enough for every free-text mzML/MGF
field seen so far, narrow enough that a multi-megabyte motif cannot swamp the
report. `load_mzml_summary` and both `compute_cosine` report shapes now carry
`UNTRUSTED_DATA_NOTE` as well.

**Residual.** Exception text still echoes the offending path; it is returned as
a plain tool error, not Markdown, and the path has already passed the security
boundary.

### 2. LOW — Traceback disclosure to the MCP client

**Confidence:** 9/10 · **Status:** fixed

`check_search_status` returned `traceback.format_exception` output for a failed
job, exposing absolute paths and internal source structure for a failure a
malformed file can trigger.

**Fix.** `tools/search.py` now reports the job's `TypeName: message` only and
states that the full diagnostic was written to the server log.
`JobResult.traceback` is unchanged; the withholding is at the tool-output
boundary, so the operator still gets the traceback in the log.

### 3. LOW — Supply-chain pinning gaps (originally overstated)

**Confidence:** 9/10 · **Status:** closed

A first pass reported "unbounded, lower-only pins, no lockfile". That was
wrong: `uv.lock` is tracked and CI syncs with `uv sync --extra dev --locked`,
so CI builds are already reproducible down to wheel hashes. Reviewing the
actual mechanism left three narrower gaps:

| Gap | Fix |
| --- | --- |
| `Makefile:install` ran `uv sync --extra dev` **without** `--locked`, so a local resolve could silently differ from the committed lockfile and from CI | re-synced with `--locked` |
| No dependency vulnerability feed: advisories against a pinned version produced no signal | `.github/dependabot.yml` for the `uv` and `github-actions` ecosystems |
| DreaMS is installed from a git URL at a moving default branch, so the embedding backend could change behaviour with no diff in this repository | pinned to commit `dbec3a0b514a99e5056cfccde4559fda8cfe8129` in `DREAMS_COMMIT`/`DREAMS_INSTALL_SPEC`; both user-facing install messages quote the pin |

`DreaMS` publishes no tags, so a tag pin was not available; the commit SHA is
the pin. `tests/test_embeddings.py::TestDreaMSSupplyChainPin` asserts the pin
stays a full SHA and that the install spec quotes it, so nothing quietly
loosens it back to a branch URL.

**Residual.** `[project.dependencies]` in `pyproject.toml` still uses lower
bounds only. That is a library-authoring convention (the lockfile is the
reproducible artifact) rather than a defect, and adding upper bounds would
create conflicts for downstream consumers. Dependabot is the update mechanism.

### 4. INFO — Dependency advisories: none outstanding

**Confidence:** 10/10 · **Status:** verified clean

`pip-audit` over the resolved environment reports **no known vulnerabilities**.
The locked set is `mcp` 2.3.0, `mcp-types` 2.3.0, `opentelemetry-api` 1.45.1
and `pillow` 13.0.0 — the versions that carry fixes for the advisories against
earlier releases of those packages.

This is worth stating explicitly because the obvious *reachability* argument is
not the reason to relax: all four `mcp` advisories are in the Streamable HTTP
transport (unbounded session accumulation), HTTP client redirect handling,
server-side bearer-token audience validation, and client-side `$ref` resolution
in output-schema validation. This server is stdio-only, does not authenticate,
and does not use `ClientSession`, so no listed vector is reachable. `pillow` is
an image codec pulled in by `massflow`→`matplotlib` and `rdkit`; nothing in
`src/` imports `PIL` or `matplotlib`, so no caller-supplied byte ever reaches
it. The fixed versions are locked anyway, so the argument does not have to
survive an attack surface that grows later.

## Verified clean

- **Filesystem boundary** — `security.resolve_path` resolves symlinks and
  checks `relative_to(allowed_root)`; library paths are validated before any
  open.
- **No dynamic execution** — no `eval`, `exec`, `pickle`, `yaml.load`, or
  subprocess use anywhere in `src/`.
- **SQL** — parameterized statements only; the synthetic library is built from
  literals and bound parameters.
- **Compressed input** — gzip/zlib decompression is bounded
  (`decompressobj`, `max_bytes + 1`, `unconsumed_tail`/`eof` checks); no
  zip/tar handling.
- **Secrets** — no credentials, tokens, or keys in tracked files; GitHub
  secret scanning and push protection are enabled on the repository, and
  `.pre-commit-config.yaml` runs `detect-secrets` locally (see below).
- **Authentication** — the server is a single-user stdio MCP server; there is
  no auth layer to bypass.

## Hardening applied after the review

Five follow-ups were raised from the file-derived-text finding. All are
implemented.

### 1. Parser fuzzing (Hypothesis)

`tests/test_parser_robustness.py` property-tests `mgf.iter_spectra`,
`mzml.iter_spectra`, both `declared_spectrum_count` implementations, and the
MSP library reader against `st.binary()` / `st.text()` input. The invariant is
the documented contract: either the bytes parse into spectra whose arrays are
finite and the same length, or the call raises `MsmcpError`. 60 examples per
property, no deadline, kept small enough for the fast suite.

**It found two real defects**, both of which made a malformed file look like an
internal server fault instead of bad input:

- `mgf._open_text` and `library._open_text` wrapped the file in a
  `TextIOWrapper` with the **locale** codec, so a Latin-1 or otherwise
  non-UTF-8 MGF/MSP raised a bare `UnicodeDecodeError`.
- `gzip.BadGzipFile` (CRC/length mismatch) and `EOFError` (truncated stream)
  surface while the parser *iterates*, not when it opens, so neither was caught
  by the existing open-time handling.

**Fix.** `security.open_text_stream(path)` pins UTF-8 decoding strictly, and
`security.malformed_stream_errors(path)` translates decode, gzip, and zlib
failures into `MalformedFileError` around the read loop — deliberately not
around the open call, and deliberately not catching `FileSizeExceededError`,
because an oversized file is not a malformed one. All three parsers now use
both helpers. Regression tests:
`tests/test_mgf.py::TestMalformedInput` (non-UTF-8 bytes, truncated gzip,
corrupt CRC) and the matching two tests in `tests/test_mzml.py`.

### 2. Supply-chain loop closure

Covered as finding 3 above: `--locked` in the Makefile, Dependabot for `uv` and
`github-actions`, and the DreaMS commit pin with a test guarding it.

Direct vulnerability scanning of the resolved environment is
`uvx pip-audit --path .venv/lib/python3.*/site-packages` (uv has no `audit`
subcommand). It is not in the gate because it needs network access; Dependabot
alerts are the continuous feed. Running it once produced finding 4.

### 3. Escaping made conventional rather than incidental

`AGENTS.md` now carries a **Report rendering** rule: any value that came from a
caller-supplied file is untrusted text, routed through `msmcp.reporting`
(`escape_inline` outside tables, `escape_table_cell` inside them, `fenced_code`
for multi-line payloads), and screen-style reports carry
`UNTRUSTED_DATA_NOTE`. `skills/untrusted-report-text/SKILL.md` is the playbook
with the helper-selection table, the anti-patterns, and the verification
recipes, so the rule survives the next person who adds a tool.

### 4. Truncation ceiling raised

`MAX_RENDERED_FIELD_CHARS` is 512, not 200. The ceiling exists to stop a
hostile field from swamping a report, not to shorten ordinary metadata, and 200
characters truncated real instrument method descriptions.
`tests/test_reporting.py` pins the value.

### 5. Secret scanning

Three layers, in increasing distance from the developer:

| Layer | State |
| --- | --- |
| GitHub secret scanning + push protection | already enabled on `janusson/MSMCP`; validity checks and non-provider patterns are currently disabled |
| `.pre-commit-config.yaml` (`detect-secrets` 1.5.0) | added; opt in with `uvx pre-commit install` |
| No secrets in tracked files | verified by scanning every tracked file except `notebooks/` and `uv.lock` (generated hex/ids that would otherwise false-positive on every edit) — zero findings |

The two GitHub settings still off are repository settings rather than code.
They are toggled in *Settings → Code security* ("Secret scanning validity
checks", "Scan for non-provider patterns"), or scripted with the REST shape
below — note that these fields are nested objects, so `gh api -f` does not
express them and a JSON body is required:

```
gh api -X PATCH repos/janusson/MSMCP --input - <<'JSON'
{"security_and_analysis": {
  "secret_scanning_validity_checks": {"status": "enabled"},
  "secret_scanning_non_provider_patterns": {"status": "enabled"}}}
JSON
```

## Verification

`make lint` (`ruff format --check`, `ruff check`, `mypy` over 75 files,
`basedpyright` with 0 errors) is clean. `make test` is **815 passed / 18
skipped**; 35 of those are new (23 in `tests/test_reporting.py` and
`tests/test_parser_robustness.py`, 12 regression tests in the existing files).
`make eval` PASSED (120 calls, 119 run, one DreaMS skip — `dreams` is
optional and not installed here). `pip-audit` over the synced environment
reports no known vulnerabilities.

Regression coverage: `tests/test_reporting.py` (helper unit tests),
`tests/test_library.py::TestRealLibrarySearch::test_library_compound_name_cannot_reshape_the_hit_table`,
`tests/test_search.py::TestUntrustedReportText`,
`tests/test_qc.py::TestQC::test_qc_file_path_cannot_inject_markdown`,
`tests/test_search.py::TestSearchDispatcherAndPoller::test_failed_job_hides_traceback_and_reports_error`,
`tests/test_parser_robustness.py` (the Hypothesis suite),
`tests/test_embeddings.py::TestDreaMSSupplyChainPin`, and
`tests/test_mgf.py::TestMalformedInput` /
`tests/test_mzml.py::TestMzMLParser` (non-UTF-8 and corrupt-gzip regression).
