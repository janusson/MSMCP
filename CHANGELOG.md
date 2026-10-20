# Changelog

All notable changes to MSMCP are recorded here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and
this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Dependency isolation

- Move the lowercase imaging `massflow` distribution out of core dependencies
  into `msmcp[imaging]` (`uv sync --extra imaging`). It is unrelated to the
  analytical MassFlow engine despite sharing its distribution name; keep those
  installations separate. Core mzML/MGF APIs and ingestion semantics are unchanged.
- Keep imzML loading lazy and report missing/incompatible imaging imports with
  source-install guidance. Preserve `ping`'s public fields and clarify that they
  inventory distribution metadata, not backend compatibility; optional absence
  is healthy. Real imaging integration tests skip in core-only environments.

### Added

- **`list_spectra` — a discovery step before loading or searching.** The new
  read-only tool indexes a file's spectra (index, MS level, retention time,
  precursor m/z, peak count) behind an optional MS-level and minimum-peak
  filter, so a caller can find the MS2 candidates in a run instead of guessing a
  spectrum index — the failure mode `search_library` already warns about when it
  is handed spectrum 0 of an LC-MS file. It reads through the same security
  boundary as the other file tools, is capped by `max_spectra`, and reports how
  many spectra it examined, so a truncated listing says so. Exercised by
  `tests/test_io.py` and the evaluation notebook.

- **Ion-mode (polarity) is reported in search results, and mismatches are
  flagged rather than filtered.** The MSP reader parses `Ion_mode` into a real
  `polarity` field on `LibrarySpectrum`, the MGF reader parses `IONMODE` into
  `Spectrum.polarity`, and both readers' providers carry it through (`Spectrum`
  gains a `polarity` field too; mzML reads the positive/negative scan cvParams).
  `search_library` threads the query's
  polarity into the report, adds a **Polarity** column to the hit table, and
  marks any hit whose declared ion mode is opposite the query's. Hits are never
  dropped on polarity: the flag lets a caller decide, and the mismatch counts as
  a caveat, not a filter. Query polarity is also recorded in the search
  provenance.

- **A user-facing front door for the documentation.** The README gains a
  **What you can ask it** section (plain-language questions → the tools that
  answer them), a **Try it in 60 seconds** quick start backed by a shipped
  worked example (`examples/caffeine_library.msp` and
  `examples/caffeine_spectrum.mgf`), and a **Glossary** of the
  mass-spectrometry and MCP terms the document uses. The maintainer-facing
  internals — the foundation-model adapters, the `JobExecutor` interface,
  developer ergonomics and the repository layout — move to a new
  **[DEVELOPING.md](DEVELOPING.md)**, so the README stays the user- and
  agent-facing story. That worked example is executed end to end by
  `tests/test_quickstart_example.py`, so a quickstart that silently stops
  working fails the build.

- **`ARCHITECTURE.md` declares v1.1** — the acceptance criteria for the next
  release, covering the fitted search null (#12), library format coverage (#2)
  and acquisition context (#16). The document is no longer titled for v1.0 and
  its intro no longer frames it as v1.0-only. Declaring the criteria does not
  meet them: the version stays `1.0.0` until a release includes the library
  reader, and `docs/roadmap.md` carries the narrative and the exclusions rather
  than a second copy of the criteria.

- **A maintained roadmap** (`docs/roadmap.md`) — where the repository is, the v1.1 acceptance
  criteria, and what is explicitly not being done. It replaces issue #11, which held the same
  content somewhere with no history, no diff and no review. The board's `Plan` field stays the live
  queue; the document is the narrative and points at the criteria themselves, which live once, in
  `ARCHITECTURE.md`. The version stays `1.0.0` until a release includes the library reader.

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

- **Scientific validation is a first-class artefact, not a claim.**
  [`SCIENTIFIC_VALIDATION.md`](SCIENTIFIC_VALIDATION.md) records, per capability,
  the claim being made, the evidence, the tolerances and their justification, and
  an explicit **VALIDATED / PARTIAL / NOT VALIDATED** status — keeping analytical
  reference calculations, independent software comparisons and experimental
  validation apart. [`VALIDATION_SIMILARITY.md`](VALIDATION_SIMILARITY.md)
  records one bounded, reproducible experiment: MSMCP's `ClassicalScorer`
  compared against `matchms` `CosineGreedy` on a pinned MassBank release
  (release, DOI and SHA-256 fixed; the dataset stays external to the repository)
  plus held-out library-recovery measurement. Similarity is **PARTIAL**;
  statistical significance and FDR remain **NOT VALIDATED**; the scoring-rule
  difference between the two greedy matchers is documented as an open limitation,
  not resolved. `tests/test_spearman.py` guards the tie-aware correlation the
  validation reports, and `make validate-similarity` runs the experiment opt-in
  (`matchms` is never a runtime dependency).

- **Isolated worktrees fail loudly instead of silently testing the wrong
  code.** `tests/conftest.py` aborts collection when `msmcp`, `pytest`, or the
  running interpreter (`sys.prefix`) resolve outside the checkout under test, so
  a run can no longer exercise one revision while importing another's package.
  `.gitignore` ignores local `.worktrees/`, and `AGENTS.md` documents the
  isolated-worktree procedure.

### Fixed

- **MGF libraries now report the ion mode the MGF reader parsed.** `MGFLibraryProvider`
  built its `LibrarySpectrum` objects without the `polarity` field, so an MGF
  library's `IONMODE` was read but dropped before the search could flag a
  polarity mismatch. The provider now carries it through, and
  `tests/test_library.py` pins it.

- **`list_spectra` reports which ceiling stopped the scan.** The listing is
  bounded by both the caller's `max_spectra` (rows) and the security policy's
  per-request spectrum budget (`MSMCP_MAX_SPECTRA`, spectra read); the footer now
  names whichever stopped it, so a truncated index is never presented as a
  complete one. Previously the scan could read the whole file to satisfy a
  filter that matched few rows.

- **The worked-example caffeine spectrum no longer contains a physically
  impossible fragment.** The synthetic query and library entry carried an
  m/z 500.1 product ion for a singly protonated 195.09 precursor, which cannot
  arise by fragmentation. The peak is removed, the files and README are labelled
  as fabricated demonstration data rather than measured spectra, and
  `tests/test_quickstart_example.py` still drives the flow end to end.

- **The byte budget now bounds what a compressed input inflates to, not only
  what it occupies on disk.** `MSMCP_MAX_FILE_SIZE_BYTES` was checked with a
  `stat` against the bytes on disk, but every reader inflates `.gz` inputs
  transparently — and a plain `.mzML` carries zlib-compressed binary arrays that
  inflate inside the parser — so a small file inside the allowed root could still
  make the server materialise an arbitrarily large blob, breaking the module's
  stated guarantee that a path inside the root cannot cause an unbounded read.
  The decompressed streams are now bounded so the ceiling applies to the bytes
  actually produced (`security.bounded_stream` for `.mzML.gz`, `.mgf.gz` and
  `.msp.gz`, and a bounded zlib inflate for mzML binary arrays), and a
  decompression bomb raises `FileSizeExceededError` instead of being read.
  Refusing the bomb and still reading a legitimate compressed input are both
  tested.

- **The server no longer tells the model that `search_library` cannot read a
  library from disk.** The `initialize` instructions still carried the
  pre-reader caveat — "still scans a synthetic in-memory library rather than the
  library file you name" — so a host that trusted its server description never
  passed an `.msp` path, and the supported-format list named only acquisition
  formats. The instructions now state that an MSP/NIST-style text library is
  read from disk through the same security boundary as an acquisition, that any
  other path is a labelled synthetic fallback, and that hits are candidate
  matches rather than identifications. A test now asserts the instructions name
  every library suffix `search_library` accepts.

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

- **The search null is fitted, not sampled (roadmap `#12`).** `search_library`
  drew 40 decoy scores per library spectrum, so the statistical cost was linear
  in the library and the empirical p-value floor `1/(n_null + 1)` capped the
  smallest expressible q at ≈0.025 — a large library could not report a strong
  match however good it was. A **fixed** decoy sample is now drawn
  (`NULL_SAMPLE_SIZE`, independent of the library) and the upper tail is fitted
  with a generalised Pareto distribution by the method of moments
  (`msmcp.models.null_model`); significance above the threshold is read from the
  fitted survival function, so a q-value below the sampled floor is expressible
  and the *null* cost is constant. The report names the model, its threshold,
  shape and scale, and the smallest attainable q, and the fit is validated
  against the sampled p-values in `tests/test_null_model.py` (disagreement
  bounded, not asserted). The exhaustive scan remains O(m); the fitted tail is
  an approximation to the decoy distribution, not a claim about a compound.

- **`ping` is registered like every other tool, so the tool-coverage guards can
  see it.** It was defined in `msmcp.server` and registered at module scope with
  a bare `@mcp.tool()`, the only tool outside the `register_tools(mcp)`
  convention the rest of the surface follows. Every guard that discovers tools
  by calling `register_tools` therefore missed it: the evaluation notebook's
  `EXPECTED_TOOLS` cross-check, `test_eval_notebook.py`'s "a registered tool is
  never exercised" assertion, and `test_tool_schemas.py`'s signature/model
  comparison all skipped it while `README.md` claimed the notebook fails if any
  registered tool is unexercised. `ping` and `PingResponse` now live in
  `msmcp/tools/system.py` behind `register_tools`, the notebook exercises
  `ping`, and `test_tool_schemas.py` names the one tool with no input model
  rather than skipping it. The published surface is unchanged: same 13 tools,
  same names, descriptions and annotations.

- **The installed SDK's Tasks limitation is asserted, not assumed.**
  `ARCHITECTURE.md` and `README.md` justify MSMCP's own polling tool with the
  claim that the installed `mcp` defines the Tasks types but omits `tasks/get`,
  `tasks/result` and `tasks/cancel` from its request unions. Nothing tested it,
  so an SDK upgrade could have falsified three maintained documents with no
  failing check. `tests/test_server_contract.py` now fails when those requests
  appear in `ClientRequest` or `ServerRequest` — which is also the trigger for
  roadmap issue #7.

- **The isotope envelope is now the exact multinomial expansion, not a
  first-order approximation.** `annotate_isotopes` computes the distribution
  from `∏(Σ aᵢ·x^Δnᵢ)ⁿ` over every element and atom, honouring the
  previously-ignored `max_isotopologue` parameter — now the wire parameter
  `isotopologues` (1–10, default 3) — so `M, M+1, … M+(n-1)` are all reachable
  and an element's full isotope ladder contributes. Two consequences: a
  chlorine- or bromine-rich formula now shows its **M+4** peak (Cl₂ ≈ 9:6:1,
  Br₂ ≈ 1:2:1) instead of being truncated at M+2; and M+2 for light molecules
  is corrected downward, because the old `p₁²/2` term overcounted when a
  dominant isotope had a low count (methane M+2 was ~9.6e-4, now ~5e-6; caffeine
  0.0094 → 0.0089, glucose 0.0147 → 0.0143). M+1 and the monoisotopic mass are
  unchanged.

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
