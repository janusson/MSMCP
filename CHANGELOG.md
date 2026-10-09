# Changelog

All notable changes to MSMCP are recorded here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and
this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **A maintained roadmap** (`docs/roadmap.md`) — where the repository is, the v1.1 acceptance
  criteria, and what is explicitly not being done. It replaces issue #11, which held the same
  content somewhere with no history, no diff and no review. The board's `Plan` field stays the live
  queue; the document is the narrative and the criteria. The v1.1 criteria are deliberately **not**
  yet promoted into `ARCHITECTURE.md`, so that document still describes v1.0 only, and the version
  stays `1.0.0` until a release includes the library reader.

- **Instrument-class defaults** — `msmcp.instruments` is the single home for the
  experimental thresholds (precursor tolerance, MS2 and diagnostic-ion windows,
  SNR bands, FDR threshold and small-library floor). Each instrument class
  (generic, orbitrap, tof, ion_trap, triple_quad) documents its own defaults and
  the basis for them, and every tool that applies one takes an `instrument_class`
  argument. Accurate-mass classes express the precursor gate in ppm; unit-
  resolution classes use a dalton window, where a ppm gate would be meaningless.

- **Real spectral-library reader (MSP/NIST text)** — `msmcp.library` reads an
  MSP/NIST-style library (`.msp`, `.msp.gz`) behind a `LibraryProvider`
  interface, and `search_library` searches it for real. `database_file` is
  resolved through the same `SecurityPolicy` as acquisitions (allowed root,
  size limit, read-only) before any open, so a library outside the allowed root
  is refused rather than read. The library is also *probed* before dispatch, so
  a file that is not the format its suffix claims raises `MalformedFileError`
  from the tool call instead of failing a background job — matching the query
  path, which is read before dispatch too. A ground-truth benchmark
  (`tests/test_library_benchmark.py`) reports per-perturbation recovery — m/z
  shift, intensity noise, dropped and added peaks — including a shift beyond
  the matching window that must fail to recover.

- **The evaluation asserts a cumulative context budget.** Section 4.12 of the
  notebook re-drives the audit's representative workflow through the real server
  object (the same `call_tool` / `model_dump_json` path the measurement script
  used), sums every result a host would receive, and requires the total to stay
  within 1.5x the audit's 20,481-byte measurement. A doubling of what a workflow
  costs in context now fails a check instead of going unnoticed.

### Fixed

- **A metadata-only MSP record no longer makes an entire library
  unsearchable.** A record with header lines but no peaks raised, so
  `GNPS-LIBRARY.msp` died 1,242 records into 15,749, and two other public
  libraries the same way (68 such records in that library alone). They are now
  skipped and counted, and the count reaches `LibraryInfo` and the search
  provenance, because a reader that drops records silently is no better than one
  that fails. The `Num Peaks` mismatch check is unchanged.

- **`search_library` no longer silently searches spectrum 0.** On a real
  LC-MS/MS file that is an MS1 survey scan — a meaningless query against an MS2
  library. `spectrum_index` selects the spectrum, the dispatch reply names what
  it will search (index, MS level, retention time, precursor m/z, peak count),
  and a spectrum with no peaks is refused before a job is dispatched.

- **mzML spectra with a legitimately empty payload are read.** A zero-length
  `binaryDataArray` is legal mzML meaning the spectrum has no peaks, written
  either as a self-closing `<binary/>` or by omitting `<binary>` entirely.
  `COE001_16ppm_5uL.mzML` (Bruker maXis QTOF) carries 4,990 empty arrays across
  2,495 of its 9,286 spectra, and the reader aborted on the third spectrum,
  losing the 6,791 that follow. Emptiness and corruption are told apart by the
  spectrum's declared `defaultArrayLength`, so an empty payload in a spectrum
  that declares peaks is still a `MalformedFileError`.

### Changed

- **The stale root artefacts are resolved** (audit F9). `MCP server
  configuration.md` is now `docs/mcp-server-configuration.md`, behind a
  provenance header stating that it is a frozen record of the reasoning rather
  than maintained documentation, and the two rendered HTML artefacts are
  relocated to `examples/` and labelled illustrative in the README. The
  `pyproject.toml` `extend-exclude` comment now describes the transcript
  accurately instead of calling it a design log.

  The HTML artefacts are **kept rather than deleted**: the criterion asked for
  their removal on the grounds that they are reproducible outputs, but no
  generator for either file exists in the repository or in the gitignored
  scratch directory, so that premise does not hold. They are one-shot session
  outputs, and relocation out of the repository root — the other option the
  issue offered — is what was done.

- Reports and provenance name the library that actually answered a search. A
  readable library (MSP/NIST text) is labelled as read from disk with its
  format, spectrum count and SHA-256 digest; a path with no reader keeps the
  synthetic fallback and is labelled as synthetic. The banner is no longer a
  fixed warning.

- **The experimental thresholds are parameters, not literals.** The precursor
  acceptance gate, the classical peak-match tolerance, the diagnostic-ion
  window, the SNR bands and the FDR / small-library thresholds now come from the
  named `instrument_class` (default `generic`, which reproduces MSMCP's v1.0
  values exactly). The class and the values actually applied are stated in the
  result — the report body for `validate_precursor`, `compute_cosine` and
  `generate_qc_summary`, and `Provenance.parameters` for `search_library` — so an
  analysis can be re-run under another instrument's assumptions and the
  assumption behind any result is readable from the result itself.

## [1.0.0] - 2026-10-08

The first tagged release. Every acceptance criterion in
[ARCHITECTURE.md](ARCHITECTURE.md) is met, and the release is cut on that basis:
the criteria are a contract, not a claim that no further work exists.

The spectral-library reader is deliberately **not** in this release — it remains
the first v1.1 deliverable (see *Known limitations* in ARCHITECTURE.md), so
`search_library` still generates a synthetic library from the `database_file`
string rather than opening it, and every report says so.

### Added

- **Core server** — an MCP server over stdio (`msmcp.server`, `protocolVersion`
  2025-06-18) exposing 13 tools with titles, annotations, `outputSchema` and
  `structuredContent`, plus a test that fails the build if any parameter reaches
  the wire undocumented.
- **Ingestion** — format dispatch with MSMCP-owned readers for `.mzML`/`.mzML.gz`
  and `.mgf`/`.mgf.gz`; `.imzML` (including its paired `.ibd`) through MassFlow.
  Vendor formats are refused with conversion guidance rather than guessed at.
- **Server-side data references** — payloads stay in the server and cross the
  wire as opaque `ptr:spectrum:<id>` tokens (registration, immutability, byte and
  count ceilings, TTL, namespaces, concurrent access).
- **Provenance** — structured, immutable, JSON-safe records tied to each
  result, so a hit table can be traced back to its inputs and library.
- **Asynchronous execution** — a pluggable `JobExecutor` interface with a local
  asyncio implementation for CPU-bound scans, with status/result/cancel, a
  concurrency cap and TTL sweeping.
- **Spectral scoring contract** — `models/scoring.py` declares `SpectrumScorer`
  (name, `score`, `score_many`) with a `ClassicalScorer` (greedy peak matching)
  and an `EmbeddingScorer` (foundation-model cosine) behind one interface, so the
  scan and its null model do not know which produced a score.
- **Spectral foundation-model adapters** — `SpectralEmbedder` contract with a
  real DreaMS inference backend, an LSM-MS2 adapter gated on a checkpoint, and
  deterministic mocks reachable only under `MSMCP_EMBEDDING_BACKEND=mock`.
- **Security boundary** — every file-reading tool is confined to one allowed root
  (`MSMCP_ALLOWED_ROOT`), with size, reference-count and spectrum-count ceilings;
  escapes, including symlinks pointing outside the root, are rejected.
- **Chemistry tools** — exact-mass and adduct-offset prediction from a fixed
  table of instrument-standard adducts, isotope-pattern annotation, ppm
  validation and QC metrics.
- **Evaluation notebook** — `notebooks/eval_msmcp.ipynb` (`make eval`) runs every
  tool end to end and writes a machine-readable report; the default test run
  stays fast by excluding it.

### Fixed

- **Spectrum scoring was normalised over matched peaks only**, so a spectrum
  sharing a single coincidental peak with the query scored exactly `1.0` —
  indistinguishable from an identical spectrum. Scores are now normalised over
  all peaks in both spectra, and the invariant is asserted.
- **The decoy set was a copy of the target set.** Decoys were built by shuffling
  the `(m/z, intensity)` pair list, which is a no-op for a scorer that sorts or
  hashes peaks, so "target-decoy FDR" compared the target distribution with
  itself. Decoys now permute intensities across the acquired m/z values, and
  spectra without a permutable intensity pattern are counted instead of silently
  reused.
- **Empirical p-values treated ties as significant**, so a spectrum scoring `0.0`
  against a null of mostly zeros was reported as significant. Ties now count, and
  the hit count is printed as `Showing the top N of M hit(s)` rather than a cap
  presented as a total.
- **The FDR branch could not report a hit at all**: the empirical p-value floor
  `1/(n_null + 1)` exceeded the q-value it was testing, and the top-ranked hit is
  charged a Benjamini-Hochberg factor. The null is now drawn at a documented
  multiple of library size, and the report states the null model it used.
- **M+1/M+2 isotope masses used the neutron mass** (1.008665 Da) instead of the
  isotope substitution difference (`¹³C − ¹²C` = 1.003355 Da), placing M+1 about
  5.3 mDa (~18 ppm at m/z 300) too high. Glucose M+1 is now 181.0668, not
  181.0721.
- **`make lint` did not run CI's format check**, so a tree that passed `make lint`
  could still fail the `gate` job — CI runs `ruff format --check .` *first*, and
  the Makefile had no target that checks formatting (`make format` applies it
  rather than verifying it). `make lint` now runs it, so the local lint target
  and CI agree on what "clean" means.

### Changed

- Reports state which half of a search is real: the query spectrum is read from
  disk, while the library is synthetic and says so in a banner ahead of any hit
  table.
- A completed search reports its result **once**. The first poll after the job
  finishes returns the full report; every later poll returns a short digest
  (library size, hit count, top hit) that keeps the synthetic-library warning,
  so a client that keeps polling no longer pulls the whole report into its
  context again. `check_search_status(job_id=..., full_report=True)` re-requests
  the report for a client that no longer has it. The delivery record is bounded
  to the 1024 most recent jobs.
- **`compute_cosine` accepts a server-side reference on either side.** The tool
  previously required both peak lists inline, so an agent that had already
  loaded a spectrum with `load_spectrum` had to push the peaks back through the
  conversation to score it. `query_peaks` / `reference_peaks` are now
  alternatives to `query_reference` / `reference_reference`, with exactly one
  source required per side (`ValidationError` otherwise), a stale or unknown
  handle raising `UnknownReferenceError` instead of scoring zero, and the
  response naming the source of each side. Dereferenced peaks take the same
  code path as inline ones, so both spellings score identically.
- The wire-contract guard in `tests/test_tool_schemas.py` now finds a numeric
  bound nested in an `anyOf` union as well as at the top level. An optional
  parameter (`X | None`) carries its keywords inside the union, so a
  top-level-only lookup would silently stop checking its bounds.
