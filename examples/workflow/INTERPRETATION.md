# Reading the report

A similarity score is not an identification. This file walks through the report
`run_workflow.py` produces and says what each part does and does not license.

## The six things the report states before the table

1. **`REAL LIBRARY — READ FROM DISK`** — the library was parsed from an actual
   file through the security boundary, and its digest is echoed so you can tell
   which file answered the query. (When a path has no reader, MSMCP substitutes a
   labelled synthetic library instead and says so.)
2. **Requested database** — the path, the format, and how many spectra were read.
3. **Query spectrum** — the file, the number of peaks, and whether it came from
   disk or a server-side reference.
4. **Query context** — MS level and precursor m/z. This is what makes the search
   context-aware rather than fragment-only: an MS1 scan is refused outright,
   because MS1 is not evidence for an MS2 library match.
5. **Scoring method and instrument class** — `classical (greedy peak matching,
   ±0.02 Da)` here, and the profile that sets the tolerances.
6. **`SIGNIFICANCE UNCALIBRATED`** — read this one twice; see below.

## The context gate, which is the part people skip

```
Context gate: precursor m/z within ±5.0 ppm; polarity, charge and adduct
compared when both query and reference state them.
Context eligibility: 9 matched, 0 unknown, 191 excluded before ranking
(reasons: precursor outside declared tolerance (191)).
```

Of 200 library records, **191 were removed before anything was scored**, because
their precursor is not within 5 ppm of the query's. That is the single biggest
effect in this workflow: the ranking only ever saw 9 candidates. A pipeline that
reports "top hit out of 200" without this line is describing a different
calculation from the one that ran.

Three states are possible per reference, and they are not interchangeable:

- **matched** — query and reference agree within the stated tolerance;
- **unknown** — the reference does not state the metadata needed to check, so it
  is kept as an exploratory candidate and labelled;
- **excluded** — it disagrees, and it is dropped, with the reason counted.

If the library states no precursors at all, the gate cannot apply and the report
says so rather than implying context was used.

## Why the top nine are all the same compound

That is the design of the exercise: the query is one measurement of a compound,
and the library holds the compound's *other* measurements. A correct search
retrieves them. The spread of scores (0.9986 down to 0.3589) is measurement
variability between acquisitions of the same substance — not nine candidates of
differing likelihood.

Which is the useful lesson: **the rank is not a confidence**, and in this
workflow it does not even distinguish compounds. Ranking *between* compounds is
a different question, and it is the one that is not yet characterized (see
below).

Be precise about what that means, because the number 200 invites the wrong
conclusion: the precursor gate removed the other **191 records — 191 distinct
compounds — before anything was scored**. The ranking only ever compared nine
measurements of one substance, so this run exercises *retrieval*, not
discrimination between competing structures. Nothing here shows that MSMCP
would prefer this compound over a structural isomer or a co-eluting analogue;
the gate made sure that question never came up.

## What the score is, mechanically

`classical` is a greedy one-to-one peak match with a ±0.02 Da window: for each
query peak in turn, take the best available reference peak within tolerance and
accumulate the intensity product, normalised over all peaks. Two consequences
you should carry into any interpretation:

- it is **order sensitive** — the greedy step sees query peaks in file order,
  which is a documented difference from `matchms` `CosineGreedy`'s maximum-product
  choice (`ARCHITECTURE.md` §Known limitations, `VALIDATION_SIMILARITY.md`);
- how much that reorders near-ties at the level of a *ranking* is not yet
  measured. Until it is, treat adjacent scores as tied.

`ClassicalScorer` was independently compared against `matchms` on a pinned
MassBank release — agreement and held-out recovery are in
`VALIDATION_SIMILARITY.md`. That comparison qualifies the scorer; it does not
qualify identification confidence.

## Why there are no p-values

The report says: *wrong-compound null admissibility and false-discovery-rate
control have not been validated; no p-values, q-values, or 5% error-rate
guarantee are provided.* This is deliberate. MSMCP fits a null from decoy scores
(`msmcp.models.null_model`), but a null fitted that way is an approximation, and
the project does not publish significance it cannot defend
(`SCIENTIFIC_VALIDATION.md` §6).

So: no threshold, no "significant hit", no FDR. If your workflow needs a
confidence statement, this tool does not provide one yet, and a higher score does
not substitute for one.

## What would make a hit an identification

None of these are in the workflow above, and none are implied by it:

- an authentic reference standard measured **on your instrument**, under your
  method, at the same collision energy;
- a retention time match under the same chromatography;
- MS/MS match criteria stated in advance (fragment count, mass accuracy,
  intensity agreement), applied to a library that states the conditions;
- an honest false-discovery estimate for the size of the search you ran.

What you get here is the retrieval step that those criteria would then be applied
to.

## What this demonstrates, and what it does not

**Demonstrates:** installation of the published package; real acquisition →
QC → context-gated search → provenance-carrying report; a gate that excludes 191
of 200 records for a stated reason; retrieval of the held-out compound's other
measurements at the top of the ranking; and a report that discloses its own
limits without being asked.

**Does not demonstrate:** compound identification, quantitative accuracy,
calibrated significance, discrimination between competing structures (the
gate removed every competing compound before scoring), or that the scorer's
ranking agrees with a reference implementation on your data.

## Reproducing and auditing

`manifest.json` records the dataset, digest, seed and the held-out compound's
InChIKey — the answer key, kept out of what the server sees. `workflow_report.json`
holds the raw payloads. Comparing the two is the check: the top-ranked compound
should be the one in the manifest, and if it is not, the search is the thing to
investigate, not the manifest.
