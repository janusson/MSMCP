# MSMCP Scientific Validation

Status of MSMCP's scientific claims, with each claim tied to evidence that
MSMCP did not generate. This document exists because *passing the test suite is
not scientific validation*: most of MSMCP's existing tests assert expected
values written next to the code they check, so they detect regressions but
cannot show the underlying science is right.

Owner: scientific validation. Audience: maintainers and the analytical-chemist
reviewer. Scope: the capabilities MSMCP ships today; it does not describe
planned work except where a gap is a release blocker.

Last updated: 2026-10-14. Applies to `main` plus the corrected isotope
implementation under review in PR #50.

---

## 1. Principles

1. **Independent evidence only.** A reference value must not come from the
   implementation under test. Re-running MSMCP and comparing it to MSMCP is not
   validation.
2. **Three complementary kinds of evidence.** They are not interchangeable:
   * **Analytical reference calculation** — an independent computation from
     authoritative physical constants (e.g. exact isotope expansion from AME2020
     masses).
   * **Independent software comparison** — agreement with a different,
     established implementation (e.g. `pyteomics` for mzML, `matchms` for
     scoring). None is wired in yet.
   * **Experimental validation** — agreement with measured instrument data.
     None is wired in yet.
3. **An LLM's opinion is never ground truth.** Neither is a second MSMCP-family
   tool: agreement between MSMCP and MassFlow is *not* independent, because they
   may share the underlying implementation.
4. **Tolerances are justified, not chosen.** Every numeric tolerance below names
   the physical or numerical reason it has the size it does.
5. **Offline-first.** Validation runs with no network, no large datasets and no
   new runtime dependencies.

### Status vocabulary

| Status | Meaning |
|---|---|
| **VALIDATED** | Every claim in the stated scope is supported by independent evidence within a justified tolerance, and the scope is the whole of the shipped behaviour for that operation. |
| **PARTIAL** | Some paths/claims are validated; other paths of the same capability are not yet covered by independent evidence. |
| **NOT VALIDATED** | The capability exists but no *independent* evidence yet supports the scientific claim (software tests may still exist). |
| **FAIL** | Independent evidence contradicts the claim. |

---

## 2. Status summary

| # | Capability | Claim scope | Status | Evidence class |
|---|---|---|---|---|
| 1 | mzML ingestion | Peaks, metadata, polarity decode per the mzML 1.1.0 CV | **PARTIAL** | Analytical reference (independent document) |
| 1b | MGF ingestion | Field parsing and ion-mode mapping | **NOT VALIDATED** | — (internal fixtures only) |
| 1c | imzML ingestion | Delegated to MassFlow | **NOT VALIDATED** | — (same-ecosystem fixture) |
| 2 | Exact mass & isotopes | Monoisotopic mass, M+1/M+2 envelope | **VALIDATED** (algorithm); constants cited | Analytical reference (independent table + exact expansion) |
| 2b | Adduct shifts | Exact Δ per ionisation pathway | **VALIDATED** | Analytical reference (CODATA constants) |
| 3 | Similarity & library search | Ground-truth recovery of the correct library entry | **PARTIAL** | Independent matchms comparison + pinned MassBank recovery (§5) |
| 4 | Statistical significance | Calibrated p/q-value under the decoy null | **NOT VALIDATED** *(known optimistic)* | Internal measurement; see §6 |
| 5 | MCP protocol integration | JSON-RPC session, tool surface, provenance plumbing | **PARTIAL** | Analytical reference (real stdio session) |

**Scope discipline.** A validation result is only as broad as the evidence behind
it. Concretely, for this document:

- The independent mzML document validates the **specific** encoding and metadata
  paths it exercises (base64 float64/float32, optional zlib, the MS level / scan
  time / selected-ion m/z / polarity accessions). It does **not** establish
  compatibility with the full range of vendor-generated mzML — writer quirks,
  Numpress, omitted-cvParam defaults and imzML remain outside it.
- The end-to-end MCP session validates the **operations it calls and the
  provenance they emit**, not provenance completeness across every operation.
- Agreement between two implementations that use the **same constants** is
  evidence of algorithmic correctness, not of the constants' scientific truth
  (§4.1).

Detail follows. §7 lists the three highest-value gaps to close next.

---

## 3. Capability 1 — Spectral parsing and ingestion

**Scientific claim.** An mzML document produced to the standard is decoded to
the exact peak arrays and metadata the producer wrote: m/z and intensity values,
MS level, retention time, precursor m/z, and ion polarity.

**Existing evidence.** `tests/mzml.py`, `tests/test_mzml.py`, `tests/test_io.py`
cover base64, 32/64-bit little-endian floats, zlib, empty arrays, declared-length
mismatch, and polarity. This is *software* evidence: every fixture is built by
`tests/conftest.py`'s own writer, so the expectations and the reader share a
lineage.

**Independent evidence.** `tests/test_scientific_validation.py::TestMzMLIndependentDocument`
builds documents with a writer **independent of the reader under test** (its own
`_MzMLWriter`, not `tests/conftest.py`'s fixtures), and checks:

* float64 peak arrays round-trip **bit-exactly** (`np.array_equal`), confirming
  no reorder, rescale or rounding;
* `TIC` equals the arithmetic sum of the intensities;
* retention time is normalised seconds → minutes (120 s → 2.00 min);
* precursor m/z, MS level and polarity are read from the correct CV accessions
  (`MS:1000744`, `MS:1000511`, `MS:1000130` positive / `MS:1000129` negative);
* unstated polarity stays `None` rather than defaulting;
* the 32-bit and zlib paths decode within float32 tolerance.

**Reference / ground truth.** The mzML 1.1.0 controlled-vocabulary definitions
(PSI-HUPO) define the accessions and the `binaryDataArray` encoding. The
independent document is authored to that standard.

**Tolerances.**
* Peak values: exact (0), because a base64 `float64` array is lossless.
* 32-bit path: `atol=1e-3` m/z — float32 has ~7 significant digits, so ~1e-4
  relative on an m/z near 200.
* Retention time: exact to the float in the document; the reader's only job is
  an exact `/60` when the unit is seconds.

**Known limitations / unvalidated assumptions.**
* The reader **does not reorder or sort** peaks; it preserves producer order.
  Validated only as "preserved", not as "sufficient for downstream scoring".
* A hidden dependency on producer behaviour remains: a document omitting the
  `64-bit float` CV (the reader then defaults to 64-bit) is not exercised by an
  independent producer.
* **MGF** (`msmcp/mgf.py`) and **imzML** (via MassFlow) have no independent
  reference document. The imzML fixture is written by `pyimzml`, the *same
  dependency MassFlow uses to read it back* — that is same-ecosystem evidence,
  not independent validation.

**Status: PARTIAL.** The specific mzML paths above are validated; the full
vendor-generated mzML range, MGF, and imzML are **NOT VALIDATED**.

---

## 4. Capability 2 — Exact mass and isotope calculations

**Scientific claim.** `annotate_isotopes` reports the monoisotopic mass and the
M / M+1 / M+2 relative abundances of a formula, with M+1 shifted by genuine
isotope substitution (¹³C − ¹²C = 1.003355 Da), not by the neutron mass.

**Existing evidence.** `tests/test_chem.py` asserts six monoisotopic masses to
1e-6 Da and several M+1/M+2 values. Its expected values live beside the
implementation and were partly authored from it; the same file also pins the
multi-carbon envelope to the binomial closed form — a genuinely independent
check that was added with PR #50.

**Independent evidence.** `tests/test_scientific_validation.py::TestIsotopeReferenceValues`,
against `tests/reference/independent_isotope_reference.py` — a separate
transcription of AME2020 masses / CIAAW abundances expanded by an exact
polynomial algorithm in `decimal` arithmetic.

**Reference values** (mass / Da; abundances relative to M = 1):

| Formula | Monoisotopic | M+1 | M+2 |
|---|---|---|---|
| CH4 | 16.031300 | 0.011276 | 0.000005 |
| H2O | 18.010565 | 0.000611 | 0.002055 |
| C2H6O | 46.041865 | 0.022702 | 0.002196 |
| C6H12O6 | 180.063388 | 0.068560 | 0.014329 |
| C7H6O | 106.041865 | 0.076781 | 0.004593 |
| C8H10N4O2 | 194.080376 | 0.103212 | 0.008941 |
| CHCl3 | 117.914383 | 0.010931 | 0.959875 |
| C6H5Cl | 112.007978 | 0.065469 | 0.321750 |
| Cl2 | 69.937705 | 0.000000 | 0.639916 |
| Br2 | 157.836675 | 0.000000 | 1.945551 |

**Result.** Monoisotopic masses agree **within <1e-6 Da**. M+1 agrees to
machine precision for every molecule. M+2 agrees to machine precision for the
C/H/N/O molecules; halogen-bearing M+2 differs by ≤1.1e-3.

**Tolerance and its justification.**
* Monoisotopic mass: `abs=1e-4 Da`. This is 0.1 mDa — far below unit
  resolution, and 100× larger than the observed disagreement, so it absorbs
  table rounding without hiding an error.
* Abundances: `abs=2e-3`. Justified by the **published-table spread** in the
  ³⁷Cl/³⁵Cl ratio, which is quoted as 0.2422 in one CIAAW transcription and
  0.2424 in another. This alone moves CHCl₃'s M+2 by ~1.05e-3 and Cl₂'s by
  ~0.7e-3 — the entire observed halogen disagreement. When both sides use the
  *same* table the **algorithm** agrees with the exact expansion to **1e-16**
  (asserted by `TestReferenceAlgorithmAgreement`); the residual is table choice,
  not a defect.

**Analytical checks with no table at all.**
* Carbon-only M+1 equals `n·p/(1−p)` and M+2 equals `C(n,2)·(p/(1−p))²`
  (binomial closed form) to `rel=1e-12`.
* The C8 M+1 shift equals ¹³C − ¹²C to 1 µDa and is >5 mDa away from the
  neutron mass — the exact bug the earlier implementation had.
* Bare Cl⁻ and Br⁻ have **zero** M+1 (no +1 isotope); a carbon-bearing chloride
  still shows a small M+1 from ¹³C/²H.

### 4.1 Constants: source, precision, and the limit of the independence claim

The reference generator uses a *separate transcription* of the same authoritative
sources the library draws on. The independence is therefore in two things — the
**transcription** (different digits/rounding) and the **algorithm** (exact
polynomial expansion vs the library's own formulation) — but **not** in the
constants themselves, which on both sides rest on AME2020 / CIAAW. This matters:
the isotope validation establishes **algorithmic correctness and transcription
robustness**, *not* that the constants are scientifically true. That last claim
rests on citing the authorities below, and could only be strengthened by
experimental isotope-ratio data.

| Constant | Value used by the reference | Source | Precision |
|---|---|---|---|
| ¹²C, ¹³C masses | 12.0000000, 13.0033548378 | AME2020 | ≤1e-9 Da (library uses 13.003354835; ~3e-9 apart) |
| ¹H, ²H | 1.00782503207, 2.0141017778 | AME2020 | ≤1e-9 Da |
| ¹⁴N, ¹⁵N | 14.0030740048, 15.0001088982 | AME2020 | ≤1e-9 Da |
| ¹⁶O, ¹⁷O, ¹⁸O | 15.9949146196, 16.99913150, 17.99916100 | AME2020 | ≤1e-8 Da |
| ³⁵Cl, ³⁷Cl | 34.968852682, 36.965902602 | AME2020 | ≤1e-9 Da |
| ¹³C/¹²C, ¹⁵N/¹⁴N, ¹⁸O/¹⁶O | 0.0107/0.9893, 0.00368/0.99632, 0.00205/0.99757 | CIAAW 2021 | 4–5 sig. figs. |
| ³⁷Cl/³⁵Cl | 0.2424/0.7576 (library: 0.2422/0.7578) | CIAAW 2021 | the inter-edition spread *is* the tolerance |
| proton / electron mass (adducts) | 1.007276466…, 0.000548579… | CODATA | ≤1e-10 Da |

**The tolerances assume the source data are correct.** The 1e-4 Da mass tolerance
and the 2e-3 abundance tolerance are sized to *published-table spread*; they
absorb a differently-rounded constant, not a wrong one.

**Outstanding evidence limitation.** The claim that the halogen residual is
*entirely* the ³⁷Cl/³⁵Cl inter-edition spread assumes both transcriptions
(0.2422 and 0.2424) are faithful to their CIAAW edition. The arithmetic
consequence was verified independently; **primary-source verification of both
digits against CIAAW remains outstanding** and would require the published
tables (not available offline).

### 4.2 Regression protection

Two mistakes have actually occurred and are pinned by explicit tests
(`TestIsotopeRegressionProtection`), so neither can return silently:

- **Neutron-mass shift.** M+1 must equal ¹³C − ¹²C (1.0033548 Da) for a
  carbon-only formula, and be >5 mDa from the neutron mass (1.0086649 Da).
- **Pooled M+2 approximation.** M+2 must equal `C(n,2)·(p/(1−p))²`, not the
  earlier `p1²/2` (for C8 the pooled value is ~14% higher, so a revert fails).

A third test pins the **abundance and mass assumptions** themselves — the
¹³C/¹²C, ¹⁵N/¹⁴N, ¹⁸O/¹⁶O, ²H/¹H, ³⁷Cl/³⁵Cl and ⁸¹Br/⁷⁹Br ratios and the
¹³C−¹²C mass. An unintended edit (a typo, a swapped digit) fails loudly with a
message naming the element; a deliberate update to a newer CIAAW edition must
update the test in the same commit, which is the point.

**Adduct shifts.** `_ADDUCT_DB` computes Δ from CODATA proton/electron masses.
Independent recomputation (m/z for `[M+H]+` = M + m(¹H⁺); `[M+2H]2+` =
(M + 2·m(¹H⁺))/2) reproduces every tabulated shift; the negative-mode adducts
follow the electron-balance convention stated in the module. This is asserted in
the suite by `TestAdductReferenceValues`, which recomputes all 14 shifts from
separately stated CODATA 2018 / AME2020 constants. **VALIDATED.**

**Known limitations / unvalidated assumptions.**
* Only single and double neutron substitution is modelled at the nominal level;
  fine structure is collapsed to an intensity-weighted mean, which is the correct
  choice for a unit-resolution instrument but **not** for a high-resolution
  profile. This is documented behaviour, not a defect, but it means MSMCP's
  envelope is not a substitute for a profile-level isotope model.
* Elements outside the tabulated set are rejected rather than estimated.

**Status: VALIDATED** for the algorithm (analytical reference), with the
constants themselves cited rather than independently re-measured — see §4.1.

---

## 5. Capability 3 — Spectral similarity and library searching

**Scientific claim.** `search_library` ranks library entries by the stated
scorer, and the top-ranked hit is the correct library member for a query drawn
from that library.

**Existing evidence.** `tests/test_library_benchmark.py` recovers known
compounds from a library it generates with the same peak generator used to build
the queries. `tests/test_quickstart_example.py` reproduces the README's worked
example. Both are **synthetic and internally generated**: the scorer, the query
and the "truth" share one source, so correct recovery is expected almost by
construction.

**Independent evidence (2026-10-14).** `VALIDATION_SIMILARITY.md` records one
bounded experiment on the pinned **MassBank `2026.03`** release (CC-BY-4.0,
SHA-256 pinned): MSMCP's `ClassicalScorer` compared against matchms 0.33.1
`CosineGreedy` on identical peak lists, then top-1/top-k recovery on held-out
spectra. Headline results — agreement is **exact on every uncontested pair**
(median |Δ| = 0, Spearman ρ = 0.9995, **tie-aware**); all divergence is explained
by window *competition*, where the two greedy rules legitimately differ (max
|Δ| = 0.42 on a single pair; consequences not yet quantified); and recovery
agrees with matchms **only on particular aggregate metrics** — the naive top-k
proportions coincide, but the strict top-5 differs (MSMCP 0.8508 vs matchms
0.8548 conditional), so "identical rankings" would be too strong. Self-matches
and near-duplicate leakage are removed, and recovery is reported on both the
answerable subset and the original denominator.

**Known limitations / unvalidated assumptions.** Recovery measures retrieval of
compounds **deliberately represented** in the library, not accuracy on absent
ones; ground truth is InChIKey equality (assumes MassBank annotations are
correct); scoring is spectral-only (precursor m/z and adduct are ignored, so
isomers confound by construction); one positive-mode ESI MS2 slice and one
matchms algorithm; agreement is a *scoring* comparison, not end-to-end pipeline
equality; the 20,000 comparison pairs are not independent observations; no
confidence interval or FDR. Similarity scores remain candidate rankings, not
identifications.

**Status: PARTIAL.** Dataset provenance (SHA-256), pre-registered tolerances,
results and the full limitation list are in `VALIDATION_SIMILARITY.md`.

---

## 6. Capability 4 — Statistical significance and the search null model

**Scientific claim.** A reported q-value is a calibrated estimate of the
false-discovery rate for the ranked hits — i.e. the fraction of hits above it
expected to be false — under a null model of a non-matching spectrum.

**What is implemented.** `msmcp/models/null_model.py` fits a generalised Pareto
upper tail (peaks-over-threshold, method of moments) to a decoy score sample,
so p-values are no longer floored by the decoy sample size. q-values apply a
Benjamini–Hochberg factor across the library size *m*.

**Existing evidence.** `tests/test_null_model.py` and the invariants in
`tests/test_scoring.py` check *internal* consistency (the fit round-trips its own
sample, degenerate tails fall back to exponential, the floor is finite and
disclosed). These are software tests. There is **no** independent statistical
evidence, and the internal evidence contradicts the claim in the extreme tail.

**Measured counter-evidence (issue #51, reproduced).** On the decoy pipeline
(`NULL_SAMPLE_SIZE = 20000`): the decoy sample is **~95 % exactly zero**
(19,042/20,000), the 10 % "tail" falls at `threshold = 0.0`, and the fit returns
`xi ≈ +0.43`. Consequences:
* The attainable floor `q_floor = p(1.0)·m` **exceeds the 0.05 FDR threshold**
  above a few thousand spectra, so no hit — even a library member used as its own
  query — is reportable at scale.
* At score 0.5 the **empirical** p is ≈1.5e-4 (two decoys ≥0.5) while the fitted
  GPD reports 6.8e-5 and an exponential fallback reports 4e-8. **The fitted tail
  is optimistic in the very region significance is claimed for.**

This is why the model must **not** be shrunk opportunistically: preferring the
exponential would certify hits on a tail the decoy sample directly refutes — a
false positive, the worse error for a scientific tool.

**Status: NOT VALIDATED.** The claim that q-values are calibrated is currently
**unsupported**, and there is positive evidence of optimism at the extreme. Do
not describe this model as validated.

### 6.1 Assumptions that must be tested empirically

1. **Zero mass is a property of the null, not of the mock generator.** The ~95 %
   zero scores may be an artefact of MSMCP's synthetic decoy generator. This is
   the single most important assumption and it is *untested on real data*.
2. **Exchangeability of decoys and targets.** q-value calibration assumes the
   decoy score distribution equals the null distribution of true non-matches.
3. **The GPD tail is the correct family** for a zero-inflated, upper-bounded
   similarity statistic (cosine ∈ [0,1] before any normalisation).
4. **Method-of-moments estimation is adequate**, as opposed to maximum
   likelihood or a robust estimator, at the sample sizes used.
5. **The BH factor over full library size *m*** is the right multiplicity
   correction for a search that also ranks.

### 6.2 Empirical evidence required before changing the model (issue #51)

* **Realistic decoy score distributions** — decoys that share m/z with targets
  (e.g. shuffled-intensity or shifted-mass decoys on a real library, as in
  target–decoy competition), so the positive tail is non-degenerate.
* **Held-out calibration** — compare the fitted survival against the empirical
  survival across the *whole* upper range on data not used for the fit, and
  **report the disagreement** (max and mean |Δ|), not merely the threshold.
* **Attainable-q measurement at 1e5 spectra** — the quantile/scale measurement
  from #12, recorded.
* **Any adopted model must track held-out empirical counts across the upper
  range**, per #51's acceptance criteria. Do not adopt a lighter tail to make the
  floor vanish.
* **Reported uncertainties.** Until this is done, the report should state the
  model is *estimated*, name its parameters, and state the smallest attainable q
  (it already does the latter).

**Datasets / provenance.** Real (or realistically simulated) decoy distributions
derived from a pinned public library (§5). Same provenance requirements.

**Status: NOT VALIDATED — release blocker for the library reader (#12/#51).**

---

## 7. Capability 5 — MCP protocol integration and provenance

**Scientific claim.** A host can drive MSMCP over the stdio JSON-RPC transport,
and every derived object carries provenance naming the operation, the exact
source file (with a content digest) and the software versions.

**Existing evidence.** `tests/test_smoke_stdio.py` runs a real child process and
checks the handshake, the tool surface and `ping`. `tests/test_provenance.py`
checks record structure. Peaks themselves are not round-tripped over the wire.

**Independent evidence.** `tests/test_scientific_validation.py::test_full_mcp_workflow_over_stdio`
launches a real server and runs `load_spectrum` → `summarise_reference` →
`annotate_isotopes` → `release_reference` as JSON-RPC, and asserts:
* the returned reference is `ptr:spectrum:…`;
* the structured result describes the spectrum (n_peaks, TIC, ms_level,
  precursor, RT in minutes) without containing peaks;
* **the provenance digest equals an SHA-256 the test computes independently from
  the file bytes** — so provenance is checked against the file, not against
  MSMCP's own report;
* `summarise_reference` reads back the same peaks the file contained (round-trip);
* the tools answer over the same session.

**Tolerances.** Exact: reference format, digest equality, peak round-trip.

**Known limitations / unvalidated assumptions.**
* Provenance records *which* reader and file, but not a scientific assessment of
  fitness; a digest proves identity, not correctness.
* The digest is intentionally skipped for files above 64 MiB
  (`DEFAULT_DIGEST_LIMIT_BYTES`); the record then reports `digest=None`. That
  behaviour is documented but not independently exercised here.
* No validation that the same operation on the same file is byte-for-byte
  reproducible across runs (determinism), beyond the digest equality above.

**Status: PARTIAL.** The transport and the provenance plumbing are validated for
the operations exercised; provenance completeness as a scientific record across
every operation is **NOT VALIDATED**.

---

## 8. Integration with the quality gate

**Current state.** `tests/test_scientific_validation.py` runs inside the existing
fast suite, so `make test` (and CI) already executes the isotope, mzML and
end-to-end MCP validation. It adds no dependency, no network and no large data.

**Proposed shape** (keep CI fast; heavy work opt-in):

| Layer | Command | Contents | Cost |
|---|---|---|---|
| Fast (every commit, CI) | `make test` | everything above; runs in seconds | low |
| Heavy / opt-in | `make validate` (proposed) | independent software comparisons (matchms), public-library ground-truth recovery, decoy-calibration measurements from #51 | minutes+, external data |

`make validate` should **not** join `make all` while external datasets are
unspecified. When it is added it must be documented as requiring pinned data via
an env var (e.g. `MSMCP_VALIDATION_DATA`), skipping cleanly when the data is
absent — mirroring how the imzML fixture already `importorskip`s its dependency.

**Gate discipline.** These tests are ordinary unit tests, not markers, so they
cannot be silently deselected; a scientific regression fails CI the same way a
code regression does.

---

## 9. The three highest-value validation gaps

1. **The search null is not calibrated (Capability 4, #12/#51).** It is the only
   capability with *counter-evidence*, and it blocks the library reader. The
   next action is data, not code: obtain realistic decoy distributions and
   measure fitted-vs-held-out empirical survival across the upper range.
2. **Similarity and library search are only partially validated (Capability 3,
   #54).** The matchms comparison and pinned MassBank recovery now exist
   (`VALIDATION_SIMILARITY.md`); still missing are other ion modes/instruments,
   `ModifiedCosine`, precursor-aware identification and confidence intervals.
3. **MGF and imzML ingestion rest on same-ecosystem evidence (Capability 1b/1c).**
   Add an independently produced reference document for each (a hand-written MGF
   is cheap; a public imzML acquisition is not, so provenance matters here too).

---

## 10. Appendix — reproduction

Run from this worktree (a linked worktree has its own `.venv`; sync it first).

```sh
# 0. Sync this worktree's environment and prove the imports resolve here.
uv sync --extra dev
uv run python -c "import msmcp, pytest, sys; print(msmcp.__file__); print(pytest.__file__); print(sys.prefix)"
#    msmcp.__file__ and pytest.__file__ must be under this checkout's src/ and
#    .venv; sys.prefix must be this checkout's .venv.  tests/conftest.py enforces
#    this and aborts collection otherwise (see PR #52).

# 1. Regenerate the independent isotope reference table (does not import msmcp).
uv run python tests/reference/independent_isotope_reference.py

# 2. Run the validation slice alone.
uv run pytest tests/test_scientific_validation.py -q

# 3. The full gate.
make lint && make test && make eval
```

**Reference data provenance.**

| Reference | Version / edition | Used for |
|---|---|---|
| AME2020 atomic masses | AME2020 | monoisotopic masses, substitution shifts |
| CIAAW representative abundances | CIAAW 2021 | M+1/M+2 abundances |
| mzML controlled vocabulary | PSI-MS, mzML 1.1.0 | accession and encoding checks |
| CODATA proton/electron masses | CODATA 2018 (as in the module) | adduct Δ |

The isotope reference is transcribed in
`tests/reference/independent_isotope_reference.py`; it is not generated from
MSMCP. No external dataset is required for the fast slice, which preserves
offline-first operation.
