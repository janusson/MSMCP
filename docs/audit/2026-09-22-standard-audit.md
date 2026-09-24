> **Provenance.** This is a dated, unedited audit of MSMCP, kept in the repository
> so that its findings and its ordered backlog have an addressable home. It was
> written on 2026-09-22 against commit `02df516` plus the then-uncommitted v1.0
> working tree; the four critical findings it reports were repaired the next
> morning in `eeeed69`, `44ac6b7` and `b77d5fe`, and the probe scripts it cites
> ran from the session scratch directory (`drafts/msmcp-audit-probes/`, not under
> version control), so those paths do not resolve inside the repository. Its
> numbers and its file inventory describe *that* tree, not the current one: read
> it as a record of reasoning, not as documentation of the present state. For the
> maintained description see [`README.md`](../../README.md) and
> [`ARCHITECTURE.md`](../../ARCHITECTURE.md).

# MSMCP: is it on track to be the standard MS MCP server?

**Question asked:** does this repo get to "research grade and industry standard" — a server that lets a
user *talk to* their mass-spec data: compare calibrations across instruments and days, pull online
MS/MS sources and network them across databases, reason about acquisition parameters, and say when a
signal is artefactual rather than annotated.

**Read from:** the repository at `/Users/ericjanusson/Programming/msmcp` at commit `02df516`
(2026-09-06) plus the uncommitted v1.0 working tree; `README.md`, `ARCHITECTURE.md`, `AGENTS.md`,
`pyproject.toml`, all 21 modules under `src/msmcp/`, the 16-file test suite, and the eval notebook's
own artefact `notebooks/.eval_artifacts/eval_report.json`.

## §0 Scope

**Verified in this session** (command in the appendix):

- 376/376 tests pass (`uv run pytest -q`, 6.93 s); `ruff`, `mypy`, `basedpyright` all clean
  (0 errors, 0 warnings); the eval notebook passes 105/106 checks, 1 honestly skipped, 14.2 s.
- The real server starts, negotiates `protocolVersion 2025-06-18`, publishes 13 tools, every
  parameter documented, `outputSchema` + `annotations` on all 13, `structuredContent` returned.
- The four science defects in §2 Findings 1–4 were **measured**, not inferred: they are reproduced by
  the probe scripts in `drafts/msmcp-audit-probes/` against the repo's own code paths.
- Capability gaps in §3 (online sources, calibration/alignment, acquisition parameters) are verified
  by exhaustive grep over `src/` plus reading the ingestion and QC modules: the *absence* is the
  evidence, and the greps are quoted.

**Not claimed:** no real spectral library was searched (none exists in the repo); no real instrument
data beyond the notebook's generated fixture was used; performance beyond the synthetic library is
extrapolated and labelled as such. The repo is left exactly as found — nothing committed, stashed or
formatted. The audit file and probes live in `drafts/`, which `.gitignore:233` excludes, and they are
*not* under version control.

**Environment note:** Python 3.13.7, `mcp` SDK **2.1.1** installed, `pydantic` 2.13.4, `numpy` 2.4.6,
`msmcp` 0.1.0. Anything this report says about the SDK is true of 2.1.1 and is *falsified by
upgrade* — see Finding 9.

## §1 Inventory

| Asset | Quantity | Evidence |
|---|---|---|
| Python, `src/` | 6,803 lines, 21 modules | `find src -name "*.py" \| xargs wc -l` |
| Python, `tests/` | 4,680 lines, 16 files | same, `tests/` |
| Largest modules | `tools/search.py` 1048, `state/pointers.py` 566, `execution/executor.py` 535, `tools/io.py` 524 | |
| MCP tools on the wire | 13 | `tools/list` over a real stdio session |
| Tests | 376 collected, 376 passed, 1 deselected (`eval`) | `uv run pytest -q` |
| Eval notebook | 106 checks, 105 PASS / 1 skip, 14.2 s | `eval_report.json` |
| Docs | `README.md` 508 lines, `ARCHITECTURE.md` 287 lines, `AGENTS.md` 25 lines | `read_file` |
| Tracked artefacts | `MCP server configuration.md` 326 KB (a Claude session transcript, 8,620 lines), `msmcp_interactive_application.html` 56 KB, `msmcp_infographic.html` 18 KB, repomix pack 620 KB (now gitignored) | `git ls-files -z \| xargs -0 du -k` |
| Uncommitted work | 23 untracked files — `state/`, `execution/`, `ingest.py`, `mgf.py`, `massflow_io.py`, `provenance.py`, `notebooks/eval_msmcp.ipynb`, 12 new test files, `AGENTS.md`, `ARCHITECTURE.md`, `Makefile` | `git status --porcelain -uall`; 48 entries in total across added/modified/deleted |

## §2 Findings

Severity is ordered by *how wrong a confident number would be*, not by how hard the fix is.

### Finding 1 — classical cosine reports 1.0000 for a spectrum sharing one incidental peak (critical)

**Symptom.** `compute_cosine(scoring_method="classical")` and `search_library`'s default scorer
normalise over *matched peaks only*, so the score is 1.0 whenever the matched pairs are proportional
— including the degenerate case of exactly one matched pair. The tool's own docstring tells the model
"Returns a cosine score between 0 and 1 (**1.0 means identical**)".

**Evidence.** `src/msmcp/tools/similarity.py` computes `score = _cosine(q_matched, r_matched)` from
`_match_peaks` output; `src/msmcp/tools/search.py:318-325` does the same. Probe:

```
_cosine([(100,1),(200,2),(300,5)], [(100.005, 9000.0)])  -> 1.0000
```

Measured against the repo's own synthetic library (4,007 spectra, query = library spectrum id 1 with
9 peaks): **53 spectra score exactly 1.0000; 52 of them had exactly one coincidental peak match**
within the ±0.02 Da tolerance. The genuinely identical spectrum also scores 1.0000. The two cases are
indistinguishable in the output: `drafts/msmcp-audit-probes/match_probe.py`.

**Cost.** In a 100 k-spectrum library, ~1 % of spectra (≈1,300) would be reported as *perfect* matches
to any query on the strength of one chance m/z coincidence inside a 0.02 Da window. Ranking is
therefore dominated by sparse matches, which is exactly backwards for library search, where query and
reference rarely have the same peak count. The threshold-free "1.0 = identical" claim is what makes
this dangerous: an agent reading the tool description will treat it as an identity.

**Correct form.** Keep greedy one-to-one pairing for *which* peaks correspond, but compute the norm
over **all** peaks in each spectrum (the standard normalised dot product / cosine with unmatched-peak
penalty). Then one matched pair out of nine scores ≈0.33, not 1.0.

### Finding 2 — the "Target-Decoy FDR" is computed against a copy of the target distribution (critical)

**Symptom.** `search_library` reports an `FDR (q-value)` column under the banner "FDR threshold
(Benjamini-Hochberg): 0.05". The null distribution it uses is built by
`rng.shuffle(list_of_(mz, intensity)_pairs)` (`search.py:562-570`) — a permutation of list order.
Every scorer is invariant to that permutation: the classical scorer sorts the reference by m/z before
matching (`_cosine`, `search.py:288-289`), the mock embedder is documented as *order-independent* by
design (`models/embeddings.py:69`), and the real DreaMS preprocessor sorts by m/z. The "decoys" are
therefore the targets.

**Evidence.** `drafts/msmcp-audit-probes/decoy_probe.py`:

```
classical: decoy==target for 20/20 spectra; max|delta|=0.000e+00
targets: mean=0.0200 p95=0.0000 max=1.0000
nulls  : mean=0.0200 p95=0.0000 max=1.0000
identical multisets: True
```

`grep -rn "decoy\|null_score\|shuffl" tests/` returns **nothing**: no test pins this behaviour, which
is why 376 green tests coexist with it.

**Cost.** Every p-value and q-value in the report is drawn from a null identical to the target
distribution, so the numbers are uninformative in principle. This is the machinery `ARCHITECTURE.md`
calls "real" and that the roadmap expects to point at a real library. Reported against real data it
would be a false-discovery narrative attached to genuine-looking chemistry.

### Finding 3 — empirical p-values treat ties as significant; 100 % of a library passes the gate (critical)

**Symptom.** `_estimate_empirical_p` (`search.py:403-419`) documents `p = (1 + #null >= s)/(1 + #null)`
but implements `side="right"`, which counts nulls *strictly greater* than `s`. Any tied score — and the
zero-scoring majority of a real library is entirely ties — is declared significant.

**Evidence.** `drafts/msmcp-audit-probes/match_probe.py`:

```
nulls: [0.0, 0.0, 0.0, 0.0, 0.9]
p for target 0.0 -> 0.333    (the documented formula gives 1.0)
```

End-to-end on the synthetic library with a query that has one true match (`cosine_probe.py`):

```
hits at q <= 0.05: 4007 of 4007      <- 100% of the library
hits at p <= 0.05: 4007 of 4007
min q-value      : 0.0145
```

**Correction (after repair).** The header line the agent sees was `Top 20 hit(s):` — the value of
`min(len(hits), 20)` — but a line *below* the table did print the true total
(`N hit(s) passed the threshold.`, `search.py:651-657`). The count was reported; this finding
overstates the concealment. The header was still wrong in kind (a cap presented as a count) and now
reads `Showing the top N of M hit(s):`.

**Cost.** The user-facing claim "statistically honest scoring — FDR control rather than 'looks similar'
vibes" (README) is currently unsatisfiable from the output. Combined with Findings 1–2 this is the
single most important repair: last week's measurement is what the roadmap is built on.

### Finding 12 — the FDR branch was structurally incapable of reporting a hit (critical; found while repairing F3)

**Symptom.** `use_fdr` is selected by library size (`n_spectra >= 2000`, `search.py:590`), and in that
branch the empirical p-values are fed to Benjamini-Hochberg over all `m` library spectra. An empirical
count over `n_null` decoys cannot express a p-value below `1/(n_null + 1)`, and with one decoy per
spectrum (`n_null = m`) the strongest hit is charged `q = p·m/1 = m/(m+1) ≈ 1`. **No hit can pass, for
any library of 2,000 spectra or more, however perfect the match.** Libraries *below* that threshold took
the p-value branch and answered sensibly, so the gate was inverted in effect.

**Evidence** (true-positive query = a library spectrum, cosine 1.0, 2,200-spectrum library;
`NULL_DECOY_MULTIPLIER` varied in-process).  "before" is the numpy scorer as first repaired; "after" is
the same sweep through the `SpectrumScorer` interface, whose classical implementation is pure Python —
`bisect` + a byte array instead of array maths, because at MS2 peak counts the numpy call overhead
dominated the arithmetic:

| decoys per spectrum | decoys scored | wall time before | wall time after | hits | smallest attainable q |
|---|---|---|---|---|---|
| 1 (the shipped code) | 2,200 | 0.23 s | 0.17 s | **0** | 0.9995 |
| 20 | 44,000 | 1.53 s | 0.67 s | 1 | 0.0500 |
| 40 (shipped now) | 88,000 | 2.90 s | 1.25 s | 1 | 0.0250 |
| 100 | 220,000 | 7.07 s | 2.97 s | 1 | 0.0100 |

Per-pair cost went from 26.3 µs to 7.4 µs (batch path) / 8.7 µs (single path) — a 3.0–3.6× reduction
that widens the affordable q-range from ≥0.05 to ≥0.01 at a few thousand spectra.  At 10⁵ spectra the
multiplier-40 null is still ~55 s per query, so the tail fit below remains the requirement for a real
library; this bought headroom, not the destination.

**Repair.** `NULL_DECOY_MULTIPLIER = 40`, decoys drawn from peaks already read for the target scan (no
extra SQL), and the report prints both the null size and the smallest attainable q so a sampling floor
cannot be mistaken for a result.

**Residual cost and scaling limit.** The null now costs `40 × m` scorer calls per query at ≈33 µs per
decoy (measured, 20-peak query), i.e. linear in library size: 2.9 s at 2,200 spectra, ~130 s at 10⁵.
Before shipping a real library reader, the null must be replaced by a **fitted upper tail** (fit a
survival function to the decoy scores and evaluate it analytically) — not implemented. Until then the
honest claim is "q-values down to ~0.025 at 2,200 spectra", not "FDR control".

**Why the null keeps the m/z channel.** Intensity permutation is deliberate: an m/z-shifted decoy scores
~0 for *every* query, so even a random query would sit above the entire null and everything would pass.
A null has to be a *plausible* spectrum, not merely a different one — the same class of error as F2,
mirrored.

### Finding 13 — every post-completion poll re-returns the whole report (medium; context budget)

**Symptom.** `check_search_status` returns a short status line while the job runs (measured: 411 bytes
pending, 441 bytes running — the handle works as designed) and the **full report** once complete, on
*every* subsequent poll as well. A host that keeps polling after completion re-ingests ~2.7 kB each time;
a harness that polled 120× accumulated 325 kB of repeated report.

**Measured context budget** (`drafts/measure_context_budget.py`: 16 calls through the real server object):
20,481 bytes total; largest single result 4,283 bytes (`generate_qc_summary`, which is aggregated and
therefore flat in spectrum count); search report 2,711 bytes. For comparison, one 100,000-peak spectrum
dumped verbatim is 3,832,276 bytes, `_summarise_spectrum` renders that same spectrum in 397 characters,
and 1.6 MB stays server-side behind a 37-character pointer.

**Repair (not yet done).** Deliver the report once; answer repeat polls with a digest. Add a cumulative
context-budget assertion to the eval, so a doubling of the total payload fails a check instead of going
unnoticed.

### Finding 4 — no spectral library reader; `database_file` is never opened (blocking, disclosed)

**Symptom.** `search_library` generates a deterministic in-memory SQLite library of 500–5,000 spectra
seeded from the `database_file` string; the path is not opened. The query spectrum *is* real.

**Evidence.** `search.py:186-232` (`_build_mock_database`), `search.py:499-512`
(`rng = random.Random(_stable_seed(database_file)); n_spectra = rng.randint(500, 5000)`); the report
banner prints "not opened (substituted a synthetic library of 4,007 spectra)"; the test
`tests/test_workflow.py::...test_the_database_path_is_never_opened` asserts the behaviour.

**Cost.** To the project's credit this is disclosed in the tool description, the dispatch reply, the
report banner, `README.md`, and `ARCHITECTURE.md` §Known limitations. It is nevertheless the reason
the server cannot yet answer any real identification question, and it is the first item on its own
roadmap. It is a *finding* here only because everything in Findings 1–3 is the machinery that will do
the work once the library is real, and that machinery is measurably broken.

### Finding 5 — zero online / cross-database connectivity (blocking for the stated product)

**Symptom.** The user's core requirement — "rapidly query user experimental data, online sources of
MSMS data, and network them together across databases" — has no implementation.

**Evidence.** Exhaustive grep over `src/` for `massbank|gnps|mona|hmdb|metlin|nist|httpx|requests\.|urllib|aiohttp`
returns **exactly one hit in Python source**: the English word "requests" in a docstring about job
requests (`src/msmcp/execution/executor.py:222`). No HTTP client is imported anywhere — the other
matches in the repo are docstring/DOI/repository URLs in `models/backends.py` and comments.
`README.md` states the server "stores no credentials, requires no API keys, and makes no network calls
of its own."

**Cost.** The distinguishing capability of the product is absent, and MassBank, GNPS, MoNA, HMDB and
NIST/MSP differ in coordinate systems, adduct conventions and licence terms — the normalisation work
is the substance, not the plumbing. Note the design tension worth keeping: "no network calls" is
currently a *security property*; connectors should be opt-in per source with an allowlist and must
carry retrieval provenance (URL, timestamp, licence) into the result.

### Finding 6 — no acquisition-parameter layer: instrument conditions are unrepresentable (blocking)

**Symptom.** The user's example reasoning — "a signal made by the source being too hot vs. something
genuinely transmitted through the collision cell" — cannot be expressed because the reader discards
every acquisition parameter that would support it.

**Evidence.** `src/msmcp/mzml.py:59-63` defines exactly four accessions read from the file:
`MS:1000576` (no compression), `MS:1000511` (ms level), `MS:1000016` (scan start time), `MS:1000744`
(selected ion m/z). `_parse_spectrum` reads only those. `grep -rn "instrumentConfiguration|
collision_energy|isolation|resolution|analyzer|source temp" src/` returns nothing. The `Spectrum`
type (`mzml.py:68-98`) has fields for index, ms level, RT, precursor m/z, mz, intensity and imaging
coordinate — nothing else survives ingestion.

**Cost.** No collision energy, no isolation window/target/offset, no instrument model, no analyzer
type or resolution, no source temperature, no polarity, no method file, no profile data. The server
cannot distinguish a 5 ppm error on an Orbitrap from one on a QqQ, cannot check precursor isolation
purity, and cannot weight a fragment by whether it cleared the collision cell. This is the layer that
would make MSMCP *more* than an MCP wrapper, and it is the one the architecture does not yet have a
slot for.

### Finding 7 — the science constants are module-level literals (research-grade blocker)

**Symptom.** Thresholds that encode the experimental question are hardcoded, so an analysis cannot be
re-run under different instrument assumptions without editing source.

**Evidence.**

| Constant | Value | Where |
|---|---|---|
| precursor acceptance | `5.0` ppm, no m/z or analyzer dependence | `tools/similarity.py:262` |
| classical peak-match tolerance | `0.02` Da default (`=20,000` ppm at m/z 1) | `tools/similarity.py:46`, `search.py:281` |
| diagnostic-ion tolerance | `_TOL = 0.02` Da | `tools/qc.py:53` |
| "low"/"high" SNR cut-offs | `< 5`, `> 20` | `tools/qc.py:98-99`, `110` |
| FDR gate / small-library threshold | `0.05`, `2000` | `search.py:519`, `575` |

**Cost.** A 5 ppm gate is generous for an Orbitrap, meaningless for a QqQ or an ion trap, and wrong
below ~m/z 200 where a unit-resolution instrument cannot resolve 5 ppm at all. Every one of these
should be a documented parameter with an instrument-class default, and the value used belongs in the
provenance record — which the architecture already has, and already populates with parameters.

### Finding 8 — no calibration, alignment, or "is there enough data" machinery (blocking for the flagship question)

**Symptom.** The user's headline question — "does this calibration from this instrument on this day
match this calibration from another instrument, and if not how do we align it, and is there enough
data for statistical certainty?" — has no code path.

**Evidence.** `grep -rn "align|calibrant|lock mass|recalibr" src/` → zero hits in source. Retention
time is read as a scalar field (`mzml.py:84`, `mgf.py:146`) and never used for alignment; there is no
replicate model, no tolerance-interval or bootstrap machinery, and no cross-run comparison tool. The
13-tool surface is entirely single-spectrum, single-file.

**Cost.** This is not a polish item; it is the product. Without it MSMCP is a competent single-spectrum
calculator. With it, it is the thing no other MCP server does.

### Finding 9 — documentation and dependency drift that will rot (high, cheap to fix)

**Symptom.** The docs encode facts about one SDK release and the repo's own state, with nothing
pinning either.

**Evidence.**

- `pyproject.toml:8` — `mcp[cli]>=2.0.0`, **no upper bound**, while `README.md:398` and
  `ARCHITECTURE.md:211-220` assert SDK-version-specific behaviour ("`mcp` 2.1.1 defines the Tasks
  types but omits `tasks/get`…"). Installed: 2.1.1. Next SDK release, those paragraphs are claims
  about a package the repo may no longer use, and no test fails.
- `pyproject.toml:6` requires `>=3.13`; `AGENTS.md:7` and `ARCHITECTURE.md` say "Python 3.12+";
  `README.md:9` badge says 3.13. Three answers to one question.
- `README.md:406` says "375 pytest cases across `tests/`"; the suite collects **376**.
- `pyproject.toml:2` version `0.1.0` while the docs describe "MSMCP v1.0 architecture" and list v1.0
  acceptance criteria that are checked off.
- The eval notebook's `EXPECTED_TOOLS` (`notebooks/eval_msmcp.ipynb`, cell 3) lists **12** tools;
  `ping` is omitted, has zero call sites in the notebook, and was never exercised — so the notebook's
  own "a registered tool is never exercised" guard has a hole exactly where the README says it doesn't.
- `MCP server configuration.md` (326 KB of agent session transcript) is tracked, and is
  `extend-exclude`d from ruff (`pyproject.toml:62`) rather than removed. `AGENTS.md` tells agents to
  read the repo before editing; this file will be read as specification.
- `.DS_Store` is tracked and modified.

### Finding 10 — distribution and reproducibility infrastructure absent (industry-standard blocker)

**Symptom.** Nothing about the repo is installable, discoverable or continuously verified by anyone
but its author.

**Evidence.** `ls .github CHANGELOG* CONTRIBUTING* server.json Dockerfile` → none exist. No CI, no
release tags (6 commits total, last 2026-09-06), no changelog, no container, no MCP registry manifest.
The official MCP registry is the ecosystem's discovery mechanism and its `server.json` requires a
publishable package (npm, PyPI or OCI) or a public remote endpoint, with namespace authentication;
PyPI publication is supported. MSMCP is published nowhere and speaks stdio only, which is
simultaneously a defensible threat-model decision (`README.md:480-486`) and the reason no hosted
client can use it.

**Cost.** "Industry standard" is decided by adoption, and adoption is gated by `npx`-equivalent
install, a registry entry, a container, and a green CI badge. Also: **the entire v1.0 body of work is
uncommitted** — 23 untracked files, including the state store, the executor, provenance, ingest, the
MGF reader and 12 test files, exist only in the working tree. One `git clean` from the audit's start
point and there is no v1.0.

## §3 Capability map against the stated product

| The user's requirement | Today | What closes it | What it deletes |
|---|---|---|---|
| "communicate with my data" | 13 tools, 4 formats (mzML, MGF, imzML, gzipped), data references + provenance | — (this half is genuinely built) | nothing |
| "query user experimental data rapidly" | real: ≈0.3–0.5 s per synthetic-library search (≈0.32 s of that is scan + decoy passes over 4,007 spectra); scorer throughput 80.6–82.5 k spectra/s on a 9-peak query | precursor-mass prefilter + binned index before any library grows past 10⁵ | the full-library linear scan |
| "online sources, network them across databases" | **absent** | opt-in connectors (MassBank REST, GNPS/MoNA exports) normalised to one `Spectrum` + retrieval provenance + a cache | the "no network calls" purity property, deliberately |
| "pull up instrument acquisition data" | **absent** — 4 cvParams read, parameters discarded | an `InstrumentContext` parsed from `instrumentConfigurationList`/`run` cvParams; no invention (AGENTS.md rule) | the assumption that a spectrum is only mz/intensity |
| "estimates on certainty as far as experimental parameters" | **absent**; `validate_precursor` is one global 5 ppm gate | parameterised tolerances + instrument-class defaults + a measurement-support score per annotation | the single hardcoded ppm gate |
| "low signal that tests well chemically can be eliminated as artefact" | **absent**; QC SNR is base peak ÷ median positive intensity, pre-collision, thresholds 5/20 | per-spectrum noise floor (median + k·MAD), fragment support after the collision cell, isolation purity where present — reported as *support*, never as certified provenance | the implication that it is decidable from centroided MS2 alone |
| "does this calibration match across instruments/day, can we align, is there enough data" | **absent** | m/z recalibration with before/after residual ppm; RT alignment with residual distribution; a comparability report with an explicit interval, not a verdict | — |
| research-grade evidence | 106 self-referential checks, synthetic library, no ground-truth benchmark | a real labelled benchmark (see §5) with a per-perturbation breakdown | the habit of quoting a passing suite as accuracy |

## §4 The recommendation that matters most

**Do not re-implement spectral scoring. Wrap it.** The repo already has the right shape for this: it
defines `JobExecutor` and `SpectralEmbedder` as interfaces with pluggable implementations, and
`ARCHITECTURE.md` states the rule "tools depend on interfaces, never on mechanisms". Scoring is the
one scientific core still implemented inline — and Findings 1–3 are all defects *inside* that
re-implementation, in ~150 lines that a mature library has spent a decade getting right.

Proposal: a `SpectrumScorer` interface (pairwise score, batch score against a library, decoy/null
model) with matchms as the first implementation — the same library MassFlow already uses — and the
current classical code demoted to a reference implementation the tests compare against. This deletes
the entire class of defect above, inherits published FDR practice (decoy generation, entropy
similarity, per-instrument tolerances), and leaves MSMCP doing what only MSMCP can do: the MCP
surface, the reference store, provenance, the executor, and the acquisition/calibration reasoning
that no library provides.

The counterargument, stated plainly: matchms' dependency weight and API churn would enter the
critical path, and its own scoring defaults are not obviously better calibrated than a careful
in-house implementation. If the in-house path is kept, Findings 1–3 must be fixed and *pinned by
tests* before any real library is attached, because the current numbers are confidently wrong rather
than absent.

## §5 Validation plan (invariants, not snapshots)

1. **Identity and dilution:** identical spectra → exactly 1.0; a spectrum sharing *k* of *n* peaks
   scores monotonically in *k*, and one shared peak of nine scores < 0.4. This test fails at HEAD.
2. **Null is distinct from target:** the decoy score distribution must differ from the target
   distribution (Kolmogorov–Smirnov p < 1e-6 on ≥10³ spectra). This test fails at HEAD.
3. **Ties:** `_estimate_empirical_p([0.0], [0.0]*4 + [0.9]) == 1.0`, per the documented formula.
   This test fails at HEAD.
4. **Counting:** the reported hit count equals the number of rows at q ≤ threshold; the report prints
   the count, not `min(count, 20)`. Currently unverifiable from the output.
5. **Real-library benchmark** (the missing evidence): a labelled query set built from MassBank/MoNA
   exports, queried against a real library, reported as top-1/top-*k* **with a per-perturbation
   breakdown** (peak removal, intensity noise, m/z jitter, precursor error) and pair-level precision
   from decoy competition. No headline accuracy without the breakdown; no accuracy figure from a
   synthetic library at all.
6. **Provenance round-trip:** every hit carries library name, version, retrieval timestamp and licence;
   every score carries the tolerance and the scorer identity that produced it.
7. **Parameterisation:** no experimental threshold remains a module literal; the value used appears in
   the provenance of the result that used it.
8. **Wire contract:** `ping` enters `EXPECTED_TOOLS`; the eval asserts the *count* of registered tools
   equals the count published, so an unexercised tool cannot hide again.
9. **Capability probe instead of prose:** a test asserts which MCP task methods the installed SDK
   actually dispatches, so the SDK-version paragraphs in the docs cannot silently rot.

## §6 Risks, including against this proposal

- **Wrapping loses control of the science.** matchms' defaults become MSMCP's defaults, and a
  dependency upgrade can change reported numbers. Mitigation: pin numerically with known-answer tests
  (the repo already does this for chemistry) and treat a scorer upgrade as a provenance-visible event.
- **Network connectors break a stated property.** "No network calls" is currently load-bearing in the
  threat model. Connectors must be opt-in, allowlisted, cached with per-source licence terms, and
  provenance-carrying — and the README threat model must be rewritten rather than quietly violated.
- **Licensing fences the best libraries.** NIST and Wiley libraries are commercially licensed;
  MassBank/MoNA are permissive but attribution-bearing. Any "networked across databases" feature needs
  a licence-aware result object from day one, not bolted on.
- **The artefact-vs-signal question is partly undecidable.** Post-hoc from centroided MS2 alone you can
  quantify *support* (noise floor, fragment count above it, isolation purity, collision-energy
  plausibility); you cannot certify that a peak came from the source. The honest product is a
  confidence report that says which parameters are missing — a server that implies more would be the
  worst failure mode for a research tool.
- **Stdio-only may be correct.** Adding remote transport contradicts the current threat model; do it
  only if hosted clients are actually the adoption path, and then with authentication, not instead of it.
- **The comparison baseline is strong.** matchms, pyOpenMS and MS-DIAL already do scoring, alignment
  and FDR. MSMCP's defensible differentiator is the *conversational interface plus evidence layer* —
  provenance, references, acquisition context — and only if those are better than what a careful user
  gets from a Jupyter notebook.

## §7 Ordered backlog

**Stop the bleeding (correctness) — DONE 2026-09-22, commits `eeeed69` + `44ac6b7`:**
1. ✅ Cosine normalised over all peaks, not matched pairs (F1); invariants in
   `TestCosinePenalisesUnmatchedIntensity`.
2. ✅ Decoys now permute intensities across the acquired m/z values (F2); `_decoy_spectrum` /
   `_permutable`, `TestNullModelIntegrity`.
3. ✅ Ties count in the empirical p-value; header reports the true count as `showing N of M` (F3).
4. ⏳ Commit the tree ✅ (`eeeed69`, 56 files). `.DS_Store` untracked ✅ (2026-09-22; it was
   already in `.gitignore`, so tracking it was an accident). **Still open:** the 8,621-line
   `MCP server configuration.md` and the root HTML artefacts (F9) — content decisions, awaiting
   the owner.
5. ⏳ Newly discovered while repairing F3: the FDR branch could never fire (F12) — repaired by the
   40× null, but the tail-fit replacement is open.

**Make it trustworthy (weeks):**
6. `SpectrumScorer` interface ✅ **DONE 2026-09-22, `b77d5fe`** — `models/scoring.py` declares the
   contract, `ClassicalScorer` and `EmbeddingScorer` implement it, and the scan and the null are
   written against it. The batch path (`score_many`) is what makes a model-backed scorer viable:
   it hoists query-side work out of the per-decoy loop, and dropping numpy for `bisect` cut the
   classical per-pair cost from 26.3 µs to 7.4 µs (2.90 s → 1.25 s per multiplier-40 search).
   **Remaining blocker unchanged in kind:** matchms cannot yet be the first real implementation —
   at 40 × m scorer calls it still needs item 9's fitted tail to be affordable on a 10⁵-spectrum
   library (~55 s per query today, against ~0.05 s of actual target scanning).
7. One real library reader — MSP/NIST-style text first, then a documented SQLite schema — plus a
   ground-truth benchmark with per-perturbation breakdowns (F4, invariant 5). **Security criterion
   that must land with it:** `search_library`'s `database_file` is currently never resolved on disk,
   so nothing validates it; the moment a provider opens it, an unvalidated path is arbitrary file
   read from an MCP tool. The provider must route the path through the same `SecurityPolicy`
   (allowed root, size limit, read-only) as acquisitions, and the eval should assert that a path
   outside the allowed root is refused.
8. Parameterise every experimental constant with instrument-class defaults and provenance (F7).
9. Fitted upper-tail null, so q-values below the sampling floor are expressible without 40 × m scorer
   calls (F12).
10. CI (uv sync → ruff → mypy → basedpyright → pytest → eval), CHANGELOG, semver tags, version → 1.0.0
    (F9, F10).
11. Deliver the search report once and answer repeat polls with a digest; add a cumulative
    context-budget assertion to the eval (F13).

**Make it the standard (quarters):**
12. `InstrumentContext`: parse acquisition cvParams that are actually present, no invention (F6).
13. Acquisition-aware confidence: neutral-noise floor, post-collision fragment support, isolation purity
    — reported as support with explicit gaps (F6, §3).
14. Calibration/alignment/comparability tools with stated intervals (F8).
15. Opt-in online connectors with licence-aware provenance (F5).
16. `server.json`, PyPI (or OCI) publication, a container, and an optional authenticated remote
    transport (F10).

## §8 Repair log — W0, 2026-09-22

| Item | State |
|---|---|
| W0.1 commit the v1.0 tree | `eeeed69` — 56 files tracked, worktree clean but for a pre-existing `.DS_Store` |
| W0.2 cosine normalisation | `44ac6b7` — both scorers, one shared semantics; invariants added |
| W0.3 decoy / null model | `_decoy_spectrum` + `_permutable`; 40 decoys per spectrum from cached peaks |
| W0.4 tie handling + honest count | inclusive comparison; `Showing the top N of M hit(s)` |
| W0.5 isotope substitution masses | glucose M+1 181.0668 (was 181.0721), M+2 182.0681 (was 182.0807) |
| F12 FDR floor | found, repaired, disclosed in the report output; tail fit still open |
| W1.1 `SpectrumScorer` interface | `src/msmcp/models/scoring.py` — contract + `ClassicalScorer` + `EmbeddingScorer` + `get_scorer`; scan and null are written against the interface |
| Scorer cost | 26.3 µs → 7.4 µs per pair (batch path); multiplier-40 search 2.90 s → 1.25 s |
| `score_many` invariant | batch path asserted equal to per-pair scoring, classical and embedding |
| Tool/scorer drift guard | `test_scoring.py` asserts `tools/similarity.py`'s score equals `ClassicalScorer`'s on random spectra |
| Tests | 406 passed, 1 deselected, 37 s (was 376 in 6.9 s — a real null model costs real time, and the scorer rewrite bought 25% of it back) |
| Lint / types | ruff, mypy (42 files), basedpyright: clean |
| Eval notebook | 103 pass / 1 skip; 2 predicates repaired (they had *required* the F1 false positive) |
| Discrimination check | true library member → 1 hit at q = 0.025; m/z-shifted query → 0 hits |
| Context budget | `drafts/measure_context_budget.py` — 16-call workflow = 20,481 bytes |
| Not done | F9 hygiene, F12 tail fit, F13 poll-once + budget assertion, F7 constants, F4 library reader |


## Addendum — "putative assignment" from accurate mass + isotope pattern

Raised after the first pass: could an m/z → formula proposal (e.g. m/z 300.06 → `C14H11F3O4`,
supported by an M+1 peak) carry a "certainty" contribution? **Yes — and it is the right feature — but
it is a different axis from Finding 1 and it does not repair it.** All numbers below are reproduced by
`drafts/msmcp-audit-probes/formula_probe.py`.

**Why it is not a fix for Finding 1.** Finding 1 is a normalisation bug in the *fragment* similarity
score: one shared MS2 peak out of nine returns 1.0000. A precursor-level formula proposal changes
nothing about that number. Worse, in a library search the precursor is normally prefilters, so every
surviving candidate shares that mass: query-side isotope evidence is then a **constant added to every
hit and it cannot reorder a single one**. Where it *can* discriminate is when the library entry itself
carries a formula (MoNA/MassBank do; MS2-only MSP records do not) — then you compare the observed
envelope against the envelope *the candidate formula predicts*.

**What the mass alone buys — measured.** Enumerating CHNOPS + halogens + Si with integer-DBE,
nitrogen-rule and valence filters around m/z 300.06:

| Tolerance | Candidates as `[M+H]⁺` | Candidates as `[M]⁺•` |
|---|---|---|
| ±2 ppm | 36 | 44 |
| ±5 ppm | 97 | 114 |
| ±10 ppm | 192 | 229 |

So at his exact m/z, 5 ppm leaves ~100 formulas. The isotope envelope is not a nice-to-have; it is the
only discriminator available at that mass. Adding "M+1 = 10 % ±25 % and M+2 < 3 %" prunes 97 → 7 and
114 → 9; at ±10 % it prunes to 2 and 4.

**The trap in the worked example.** `C14H11F3O4` is monoisotopic **300.0609**, DBE 8 — it fits
m/z 300.06 as `[M]⁺•` (0.3 ppm) but **not** as `[M+H]⁺`, which would require a neutral of 299.0527.
Its predicted envelope is **M+1 = 15.42 %** (13C-heavy: 14 × 1.082 %, plus 17O 0.15 %, 2H 0.13 %) and
M+2 = 1.93 %. An observed, clean 10 % M+1 therefore *disagrees* with this formula by ~35 % relative,
and would be read as roughly **nine carbons** (10 % ÷ 1.07 % per C), not fourteen. "There is an M+1"
is not the test; a quantitative envelope fit with a stated tolerance is, and the tolerance has to
account for intensity measurement quality.

**New finding (### Finding 11 — the existing isotope calculator reports wrong M+1/M+2 masses).**
`tools/chem.py:231,241` compute `m1_mass = mono_mass + NEUTRON_MASS` and `m2_mass = mono_mass + 2 ·
NEUTRON_MASS`. The neutron mass (1.008665) is not the isotope *substitution* mass difference: 13C
differs from 12C by 1.003355. Measured against exact masses:

| | repo `annotate_isotopes` | exact | error |
|---|---|---|---|
| M+1, C14H11F3O4 | 301.0696 | **301.064298** | **+5.30 mDa (+17.6 ppm)** |
| M+2, C14H11F3O4 | 302.0783 | 302.066643 (centroid) | +11.7 mDa |
| M+1 abundance | 0.1542 | 15.4209 % | correct |
| M+2 abundance | 0.0201 | 1.9291 % | correct to its stated approximation |

The abundances are right; the **positions are wrong by more than the tool's own 5 ppm acceptance
gate**, so an observed M+1 or M+2 centroid could never match a prediction this system makes — and the
same error (13C +5.31, 15N +11.63, 17O +4.45, 18O +11.1, 34S +5.6, 37Cl +2.0, 81Br +1.3 mDa) would
poison any envelope-fitting confidence layer built on top of it. Fix: enumerate substitution mass
differences per isotope, and report the fine structure as well as the unresolved centroid — at
m/z 301 the 13C and 15N components sit 6.3 mDa (21 ppm) apart, which is the resolution question that
needs Finding 6's instrument context to answer.

**Design that fits this repo.** Three pieces, none of which is a similarity score:

1. `annotate_formula_candidates(mz, ppm, charge/adduct, element_ranges)` → candidates with DBE, ppm
   error, and the predicted envelope. Deterministic, no model, testable against known formulas.
2. `score_isotope_envelope(observed_peaks, formula, instrument_context)` → residual between observed
   and predicted M/M+1/M+2 with an explicit tolerance, **gated on measurability**: peak above the
   local noise floor, no saturation, no co-isolation, and a resolution that supports whatever fine
   structure it claims to use. Not measurable must report "not determinable", never "failed" — this
   is precisely the artefact question from the original brief, and the honest answer is often
   "the parameter set does not decide this".
3. A confidence composer that emits **typed evidence**, not one opaque number, aligned with the
   community's existing vocabulary (Schymanski et al. 2014, ES&T 48(4):2097, DOI 10.1021/es5002105):
   level 4 = formula from accurate mass + consistent isotope pattern; level 3 = tentative candidates;
   level 2 = MS2 library match; level 1 = reference standard with MS2 + retention time. That paper
   also states the constraint this audit keeps returning to: comparing spectra recorded under
   different acquisition parameters (resolution, collision energy, ionisation, MS level) is exactly
   where matches become invalid.

**Ordering constraint.** Do not fold a new evidence channel into the search score before Findings 1–3
are fixed and the null model is real: the p-values are already computed against a copy of the target
distribution, so any composed score inherits a meaningless calibration. Sequence: fix the score →
fix the null → measure the isotope term on its own → then compose and recalibrate.



## Appendix — reproduce every figure

Probe scripts (run from the repo root; they import the repo's own code paths):

```bash
cd /Users/ericjanusson/Programming/msmcp
uv run pytest -q                                  # 376 passed, 1 deselected in 6.93s
uv run ruff check . ; uv run mypy ; uv run basedpyright
uv run pytest tests/test_eval_notebook.py -m eval -s -q   # 105 PASS / 1 skip, 14.20 s
uv run python drafts/msmcp-audit-probes/decoy_probe.py    # decoy == target, identical multisets
uv run python drafts/msmcp-audit-probes/match_probe.py    # 53×1.0, 52 single-peak; p(0.0)=0.333
uv run python drafts/msmcp-audit-probes/formula_probe.py  # M+1 mass error +5.30 mDa; 97/114 candidates at 5 ppm
uv run python drafts/msmcp-audit-probes/cosine_probe.py   # 4007/4007 at q<=0.05; cosine=1.0 on 1 match
uv run python drafts/msmcp-audit-probes/wire_probe.py     # 13 tools, protocolVersion 2025-06-18
uv run python drafts/msmcp-audit-probes/bench_probe.py    # 80.6k-82.5k spectra/s, 9-peak query
uv run python drafts/msmcp-audit-probes/time_probe.py     # 4007-spectrum scan in 0.45 s (0.32 s scan+decoy)
find src tests -name "*.py" | xargs wc -l         # 6803 src + 4680 tests
grep -rn "httpx\|requests\.\|urllib\|aiohttp\|massbank\|gnps\|mona" src/   # 1 hit: word in a docstring
grep -rn "decoy\|null_score\|shuffl" tests/       # no output: the FDR path is untested
git ls-files | wc -l                              # 34 tracked files
git status --porcelain -uall | grep -c '^??'      # 23 untracked files
wc -lc "MCP server configuration.md"              # 8620 lines, 326461 bytes
```

Report written 2026-09-22; evidence read at commit `02df516` plus the uncommitted working tree. This
file and its probes live in `drafts/`, which `.gitignore:233` excludes, so they will not appear in
`git status` and must not be committed.
