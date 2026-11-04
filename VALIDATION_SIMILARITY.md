# Similarity & library-search validation — issue #54

One bounded, reproducible experiment answering three **separate** questions about
MSMCP's spectral scoring. Status: **PARTIAL** — one slice evaluated on one
pinned public library; not a platform, and not a claim about all data.

1. **Implementation agreement** — does `msmcp.models.scoring.ClassicalScorer`
   compute the same cosine as the independent, established `matchms`
   `CosineGreedy` on identical peak lists?
2. **Identification recovery** — on held-out spectra, does MSMCP's ranking
   recover the correct compound (top-1 / top-k), with self-matches and duplicate
   leakage removed?
3. **Ranking impact** — on these same selected queries/references, how often do
   the two greedy matching rules select different records/compounds or change
   whether the known compound appears in top-k?

Agreement is **not** accuracy: two implementations agreeing says nothing about
whether either identifies the right compound. The experiment keeps them apart
deliberately.

Harness: `validation/similarity_validation.py` (opt-in; not collected by
`pytest`, not in the fast CI suite). Existing agreement/recovery result:
`validation/massbank-2026.03-results.json`. Qualification ranking extension:
`validation/massbank-2026.03-ranking-qualification.json`.

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
* **Agreement** measured on 20,000 seeded (query, database) pairs. These are
  drawn with replacement from a 250 × 3,499 grid, so they are **not** 20,000
  independent observations.
* **Two denominators for recovery.** Near-duplicate filtering can remove a
  query's only same-compound reference; recovery is therefore reported
  *conditionally* (among answerable queries) **and** with the **original
denominator** (all queries, an excluded query counting as a miss). The
  original-denominator figure is the population-stable one for comparing
variants.
* **Tolerances were fixed before results were read:** uncontested pairs
  (no window competition) must agree to ≤ 1e-9; median |Δ| ≤ 0.01, p95 ≤ 0.10,
  Spearman ρ ≥ 0.99 (**tie-aware**, average ranks).

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
| Spearman ρ (**tie-aware**) | **0.999459** | ≥ 0.99 |
| contested pairs | 2,273 (1,398 differ) | — |
| **uncontested pairs that differ** | **0** | must be 0 |

**All disagreement is explained by matching contests.** Where the matching is
unambiguous the two implementations agree to ≤ 1e-9 on every pair. The
correlation uses **average ranks**; the earlier `argsort(argsort)` idiom gave
tied scores arbitrary distinct ranks and reported ρ = 0.999426. The corrected
value is reported (the old number was not preserved). See `tests/test_spearman.py`
for the tie behaviour and a regression case where the naive idiom inflates a
true 0.914 to 1.0.

### 2. Identification recovery (self-matches and identical duplicates removed)

`strict` additionally drops entries scoring ≥ 0.995 to the query; this can make a
query unanswerable, so both denominators are shown.

| Scorer | variant | answerable | excluded | conditional top-1/5/10 | original-denominator top-1/5/10 |
|---|---|---|---|---|---|
| **MSMCP** | naive | 250/250 | 0 | 0.7360 / 0.8520 / 0.8880 | 0.7360 / 0.8520 / 0.8880 |
| **MSMCP** | strict | 248/250 | 2 | 0.7379 / 0.8508 / 0.8871 | 0.7320 / 0.8440 / 0.8800 |
| matchms | naive | 250/250 | 0 | 0.7360 / 0.8520 / 0.8880 | 0.7360 / 0.8520 / 0.8880 |
| matchms | strict | 248/250 | 2 | 0.7379 / 0.8548 / 0.8871 | 0.7320 / 0.8480 / 0.8800 |

MSMCP recovers the correct InChIKey in the top-1 hit for **73.6 %** of the 250
queries (top-10: 88.8 %). The two scorers agree **exactly** on the *naive top-k
proportions*, but **not on every metric**: MSMCP's strict top-5 is 0.8508
(conditional) / 0.8440 (original denominator) against matchms's 0.8548 / 0.8480 —
a **1-in-250 (0.4 pp)** difference. So "identical rankings" is too strong; what
is supported is exact agreement in particular aggregate metrics at this scale.
Near-duplicate strictness moves recovery by ≤ 0.014: leakage is small but not
zero.

### 3. Paired ranking impact (qualification extension)

The qualification reran the same pinned bundle, seed, query split and scorer
matrices, then compared stable database-order rankings directly. In the naive
variant, the top-1 **reference record** differed for 11/250 queries (4.4%); the
top-1 **compound identity** differed for 6/250 (2.4%). The top-1 known-compound
hit status differed for 6 queries: MSMCP-only 3 and matchms-only 3. Thus equal
aggregate top-1 proportions (184/250 for both) conceal per-query disagreement.
At top-5, known-compound hit status agreed on all 250 naive queries; at top-10,
it differed for 2 (one scorer-only hit each). Under the strict ≥0.995 near-
duplicate filter, top-5 hit status differed for 1 query (matchms-only), consistent
with the existing 1-in-250 aggregate top-5 difference. Mean top-5 reference-record
set Jaccard was 0.9538 (naive) and 0.9498 (strict).

These are descriptive paired results on this selected MassBank slice only. The
bundle does not identify acquisition batches, so no batch-clustered uncertainty
interval is estimated. The query/reference construction is an in-library retrieval
task; these numbers do not estimate open-set accuracy, FDR, or production search
performance across instruments.

## Interpretation

* **Implementation agreement: PASS.** MSMCP's cosine is a faithful greedy
  cosine — numerically exact against matchms whenever the greedy matching is
  unambiguous, and rank-correlated at ρ = 0.9995 (tie-aware) overall. The one
  non-exact quantity is the *score* of contested pairs, not the ranking of
  ordinary ones.
* **Recovery agreement: partial.** MSMCP and matchms tie on the naive top-k
  proportions but differ on the strict top-5 (0.8508 vs 0.8548 conditional;
  0.8440 vs 0.8480 original denominator). Agreement is metric-specific, not
  universal.
* **The greedy-rule discrepancy is structural and consequential.**
  `ClassicalScorer` matches nearest-m/z **in query order**; `CosineGreedy`
  matches **maximum intensity product** first. Minimal reproduction —
  `query = [(100.0, 10.0), (100.01, 20.0)]`, `reference = [(100.0, 15.0)]`,
  ±0.02 Da: MSMCP `0.4472` vs matchms `0.8944` — a **twofold** score difference.
  In this one pinned slice it changed the top-1 reference record on 11/250
  queries and top-1 compound on 6/250; top-k true-compound hit discordance was
  0/250 naive at k=5, 2/250 naive at k=10, and 1/250 strict at k=5. These are
  observed retrieval differences, not grounds to replace MSMCP's scorer or to
  claim either scorer is more accurate. Ranking impact outside this slice remains
  unmeasured. **No production algorithm change is made.**
* **Identification accuracy: ~74 % top-1 on this protocol.** A property of the
  *method and data*, not proof that 74 % of reported hits are true
  identifications.

## Limitations and unvalidated assumptions

1. **Retrieval of compounds deliberately present in the library.** Every query
   compound is represented in the database by ≥ 1 *different* measurement, by
   construction. This is **not** an estimate of accuracy on compounds absent
   from the library — the hard, realistic case.
2. **Ground truth is InChIKey equality.** It assumes MassBank annotations are
   correct; misannotated records count as failures (or, for a query, are
   excluded).
3. **Spectral-only identification.** `ClassicalScorer` ignores precursor m/z,
   adduct and collision energy — two isomers with similar MS2 will confound it
   by construction. No precursor gating is applied here.
4. **One library, one slice.** MassBank `2026.03`, positive-mode ESI MS2, ≤ 300
   peaks, precursor 100–1200 Da; other ion modes, instrument classes and the
   full 139k-record set are untested.
5. **Agreement ≠ end-to-end equality.** Identical peak lists are fed to both
   scorers, isolating the *scoring* step. It does **not** test matchms's own MSP
   parsing, nor MSMCP's full search pipeline (null model, FDR, report).
6. **The 20,000 agreement pairs are not independent observations** — sampled with
   replacement from a 250 × 3,499 grid, with queries and references recurring
   across pairs. Treat them as a descriptive distribution, not 20,000 trials.
7. **No calibrated confidence.** Recovery is a point proportion: no confidence
   interval and no FDR here (that is #12/#51, deliberately separate).
8. **One matchms algorithm.** Only `CosineGreedy`; `ModifiedCosine` and the
   embedding scorers are unvalidated.
9. **Tolerances are agreement tolerances**, chosen to admit the known
   greedy-rule difference; they are not accuracy thresholds.

## Reproduction

```sh
# 1. Fetch the pinned dataset (outside the repo) and verify its SHA-256.
curl -L -o /tmp/MassBank_NISTformat.msp \
  https://github.com/MassBank/MassBank-data/releases/download/2026.03/MassBank_NISTformat.msp
shasum -a 256 /tmp/MassBank_NISTformat.msp   # cf49e1b2…fc269

# 2. Run the experiment (matchms supplied per-command only).
uv run --with "matchms==0.33.1" python validation/similarity_validation.py \\
    --data /tmp/MassBank_NISTformat.msp --out validation/massbank-2026.03-ranking-qualification.json
#    or: make validate-similarity MSMCP_VALIDATION_DATA=/tmp/MassBank_NISTformat.msp
```

## Status

**PARTIAL.** One reproducible slice now records implementation agreement,
in-library recovery, and paired ranking differences against matchms on the pinned
public library. Still to do: other ion modes/instruments, `ModifiedCosine`,
precursor-aware identification, acquisition-batch grouping/uncertainty, open-set
identification, and the statistical calibration tracked separately in #12/#51.
