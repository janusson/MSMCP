# Changelog

All notable changes to MSMCP are recorded here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and
this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
`pyproject.toml` currently declares version `0.1.0`; the first tagged release is
planned as `1.0.0` once the acceptance criteria in
[ARCHITECTURE.md](ARCHITECTURE.md) are met. Everything below is unreleased work
accumulated on `main`.

## [Unreleased]

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
