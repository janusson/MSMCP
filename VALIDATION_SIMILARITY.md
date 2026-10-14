# Similarity & library-search validation — issue #54

One bounded, reproducible experiment answering two **separate** questions about
MSMCP's spectral scoring. Status: **PARTIAL** — one slice validated on one
pinned public library; not a platform, and not a claim about all data.

1. **Implementation agreement** — does `msmcp.models.scoring.ClassicalScorer`
   compute the same cosine as the independent, established `matchms`
   `CosineGreedy` on identical peak lists?
2. **Identification recovery** — on held-out spectra, does MSMCP's ranking
   recover the correct compound (top-1 / top-k), with self-matches and duplicate
   leakage removed?

Agreement is **not** accuracy: two implementations agreeing says nothing about
whether either identifies the right compound. The experiment keeps them apart
deliberately.

Harness: `validation/similarity_validation.py` (opt-in; not collected by
`pytest`, not in the fast CI suite). Results: `validation/massbank-2026.03-results.json`.

## Dataset and provenance

| Field | Value |
|---|---|
| Library | **MassBank** (MassBank-data official records) |
| Release | **`2026.03`** (GitHub release tag; published 2026-04-15) |
| Asset | `MassBank_NISTformat.msp` (137,309,196 bytes) |
| URL | `https://github.com/MassBank/MassBank-data/releases/download/2026.03/MassBank_NISTformat.msp` |
| Licence | **CC-BY-4.0** (per `CITATION.cff`; attribution to the MassBank consortium required) |
| DOI | `10.5281/zenodo.3378723` |
| SHA-256 | `cf49e1b293f1c8c1b4a645123638826eea70db656bd76e09ea0c47a1a56fc269` |

The file is verified against the SHA-256 before anything is scored; a changed
file aborts the run. The dataset is **never committed** — it is supplied through
`MSMCP_VALIDATION_DATA` / `--data`.

**Preprocessing (deterministic; run after SHA verification).** From the 139,006
records, keep those that: state an `INCHIKEY`; are `Ion_mode: POSITIVE`;
`Spectrum_type: MS2`; have `5 <= peaks <= 300`; and have a precursor in
[100, 1200] Da. → **59,061 records**, 20,332 distinct InChIKeys.

## Method

* **Scorers:** MSMCP `ClassicalScorer` and matchms `CosineGreedy`, both at
  **±0.02 Da** (MSMCP's generic MS2 window), fed the **same** peak lists so only
  the scoring algorithm differs.
* **Held-out split (`seed 20261014`).** Group by InChIKey; keep compounds with
  ≥ 2 *distinct* peak lists. Take 250 compounds; the query is that compound's
  record with the most peaks, and its **other** records stay in the database, so
  a correct answer requires a genuinely different measurement — not the query
  itself. Database = those in-group records + a seeded sample of distractor
  compounds → **3,499 references**. Records whose peak list is *identical* to a
  query are removed (self-match / duplicate leakage guard).
* **Strict variant.** Before ranking, database entries scoring ≥ 0.995 to the
  query are dropped, removing near-duplicate leakage. Reported alongside the
  naive number.
* **Agreement** measured on 20,000 seeded (query, database) pairs.
* **Tolerances were fixed before results were read:** uncontested pairs
  (no window competition) must agree to ≤ 1e-9; median |Δ| ≤ 0.01, p95 ≤ 0.10,
  Spearman ρ ≥ 0.99.

`matchms==0.33.1` is supplied per-command only (`uv run --with`); it is **not** a
project dependency, so the runtime and the fast CI suite never install it.

## Results (matchms 0.33.1, 20,000 pairs, 250 queries, 3,499 references)

### 1. Implementation agreement

| Metric | Value | Tolerance |
|---|---|---|
| median \|Δcosine\| | **0.000** | ≤ 0.01 |
| 95th percentile \|Δ\| | **1.81e-4** | ≤ 0.10 |
| max \|Δ\| | 0.424 | — |
| exact agreement (\|Δ\| ≤ 1e-9) | **93.0 %** | — |
| Spearman ρ | **0.99943** | ≥ 0.99 |
| contested pairs | 2,273 (1,398 differ) | — |
| **uncontested pairs that differ** | **0** | must be 0 |

**All disagreement is explained by matching contests.** Where the matching is
unambiguous the two implementations agree to ≤ 1e-9 on every pair.

### 2. Identification recovery (self-matches and duplicates removed)

| Scorer | variant | top-1 | top-5 | top-10 |
|---|---|---|---|---|
| **MSMCP** | naive | 0.7360 | 0.8520 | 0.8880 |
| **MSMCP** | strict | 0.7379 | 0.8508 | 0.8871 |
| matchms | naive | 0.7360 | 0.8520 | 0.8880 |
| matchms | strict | 0.7379 | 0.8548 | 0.8871 |

MSMCP recovers the correct InChIKey in the top-1 hit for **73.6 %** of held-out
queries (top-10: 88.8 %), and **matches matchms exactly** — the algorithm
difference does not change identification at this scale. The strict (near-
duplicate-removed) numbers are essentially the same, so duplicate leakage is
negligible in this design.

## Interpretation

* **Implementation agreement: PASS.** The MSMCP cosine is a faithful greedy
  cosine; it is numerically exact against matchms whenever the greedy matching
  is unambiguous, and rank-correlates at ρ = 0.9994 overall.
* **The one real discrepancy is structural, not a bug.** `ClassicalScorer`
  matches query peaks **in query order, each to the nearest free reference
  peak**; `CosineGreedy` matches the **highest intensity product first**. When a
  reference peak is within the window of several query peaks (or vice versa),
  they choose differently and can diverge by up to ~0.42 on a single pair.
  Minimal reproduction — `query = [(100.0, 10.0), (100.01, 20.0)]`,
  `reference = [(100.0, 15.0)]`, ±0.02 Da: MSMCP `0.4472`, matchms `0.8944`.
  MSMCP's rule can *understate* similarity relative to matchms; whether to match
  on nearest-m/z or maximum-product is a design choice worth revisiting, but it
  is documented behaviour and is out of scope here.
* **Identification accuracy: ~74 % top-1 on this protocol.** This is a property
  of the *method and data*, not proof that 74 % of reported hits are true
  identifications.

## Limitations and unvalidated assumptions

1. **Ground truth is InChIKey equality.** It assumes MassBank annotations are
   correct; unannotated/misannotated records count as failures (or, for the
   query, are excluded).
2. **Spectral-only identification.** `ClassicalScorer` ignores precursor m/z,
   adduct and collision energy — two isomers with similar MS2 will confound it
   by construction. No precursor gating is applied here.
3. **Subset, not the library.** 250 query compounds and 3,499 references from one
   positive-mode ESI MS2 slice; other ion modes, instrument classes and the full
   139k-record set are untested.
4. **Agreement ≠ end-to-end equality.** The comparison feeds identical peak lists
   to both scorers, so it isolates the *scoring* step. It does **not** test
   matchms's own MSP parsing, nor MSMCP's full search pipeline (null model, FDR,
   report).
5. **No statistical control.** Recovery is a point proportion; there is no
   confidence interval and no FDR here (that is #12/#51, deliberately separate).
6. **One matchms algorithm.** Only `CosineGreedy` was compared; `ModifiedCosine`
   and the embedding scorers are unvalidated.
7. **Tolerances are agreement tolerances**, chosen to admit the known
   greedy-rule difference; they are not accuracy thresholds.

## Reproduction

```sh
# 1. Fetch the pinned dataset (outside the repo) and verify its SHA-256.
curl -L -o /tmp/MassBank_NISTformat.msp \
  https://github.com/MassBank/MassBank-data/releases/download/2026.03/MassBank_NISTformat.msp
shasum -a 256 /tmp/MassBank_NISTformat.msp   # cf49e1b2…fc269

# 2. Run the experiment (matchms supplied per-command only).
uv run --with "matchms==0.33.1" python validation/similarity_validation.py \
    --data /tmp/MassBank_NISTformat.msp --out validation/massbank-2026.03-results.json
#    or: make validate-similarity MSMCP_VALIDATION_DATA=/tmp/MassBank_NISTformat.msp
```

## Status

**PARTIAL.** One substantive, reproducible slice: implementation agreement
validated against an independent implementation, and identification recovery
measured on a pinned public library with leakage guards. Still to do: other ion
modes/instruments, `ModifiedCosine`, precursor-aware identification, confidence
intervals, and the statistical calibration tracked separately in #12/#51.
