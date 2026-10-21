# README scientific demonstration audit — 2026-10-20

Inspected implementation: upstream `main` at
`7d3e8a4f335ee25d4e4f3c600bf075ba02382c6a`. The dirty canonical checkout
contains separate significance-safety work and is deliberately excluded. This
branch changes documentation and example tooling, not scientific algorithms.

## Findings and repairs

| README area | Finding | Repair |
|---|---|---|
| Data-reference flow | `metabolomics.db` selects synthetic substitution, not a database reader | Supported MSP placeholder, explicitly illustrative; no fictional numeric output |
| Quick-start caffeine example | Both query and library contain fabricated peaks; identical peaks guarantee the displayed top score | Replace advertised demo with executable public mzML and measured MassBank workflow; existing fixtures stay labelled synthetic |
| Agent walkthrough | Fictional mzML metadata, job IDs, hit table and agent response; the final sodium inference is unsupported, and neutral/ion comparison is ambiguous | Remove the invented conversation; link actual transcript and full-precision ion calculation |
| Other illustrative snippets | Unrecorded chemistry/provenance output and an invented DreaMS score | Label schema/calls illustrative; remove the claimed embedding score and fictional chemistry dialogue |
| Input-format failure description | “Cannot read” conflates unsupported suffixes with missing/malformed supported files | State the two different outcomes explicitly |
| Scientific-validation summary | Claimed mzML ingestion validated without its documented partial scope | Match the PARTIAL status in `SCIENTIFIC_VALIDATION.md` |
| Architecture | Still calls MGF libraries planned despite the shipped provider | Correct format status; retain SQLite as planned |

## Implementation evidence

- [`library.py`](../../src/msmcp/library.py): `_provider_class_for` recognizes
  `.msp`, `.msp.gz`, `.mgf`, `.mgf.gz` case-insensitively. `get_library_provider`
  resolves the path through `SecurityPolicy`, checks file size/readability and
  opens read-only. MSP validates declared peak counts and parsing; MGF delegates
  to the acquisition reader. Neither parser proves experimental origin.
- [`tools/search.py`](../../src/msmcp/tools/search.py): `_library_provider_for`
  returns `None` for unsupported suffixes. `_run_scan` then builds a deterministic
  generated library, seeded from the string, containing 500–5,000 spectra.
  Unsupported paths are not opened; even a nonexistent `.db` name can dispatch.
  Supported paths are probed before dispatch: missing, unreadable, malformed or
  boundary-violating files fail rather than substituting generated spectra.
  Dispatch, `_report_banner`, and `_search_provenance` distinguish the routes.
- The same search implementation still fits a decoy null and filters with
  empirical p-values or BH q-values. **These do not establish calibrated
  wrong-compound significance/FDR.** The recorded one-entry smoke check retains
  the actual report, including its rounded `0.0000` p-value, rather than altering
  a scientific output for presentation. This value must not be marketed as zero
  probability of error. See [scientific status](../../SCIENTIFIC_VALIDATION.md).
- [`tools/io.py`](../../src/msmcp/tools/io.py): `load_spectrum` returns structured
  metadata, an opaque reference and source/software provenance. `summarise_reference`
  derives its compact view from stored peaks. The demo verifies the returned file
  digest against independently hashed input bytes.
- [`tools/similarity.py`](../../src/msmcp/tools/similarity.py):
  `validate_precursor` calculates absolute difference divided by theoretical mass
  for ppm classes and applies the instrument profile window. It receives the
  expected ion m/z explicitly; it does not infer formula/adduct or test identity.
- [`tools/chem.py`](../../src/msmcp/tools/chem.py): formula and adduct tools return
  rounded text. The demonstration uses independently transcribed constants for
  the full-precision ion calculation rather than treating rounded text as exact.

## Scientific evidence retained, not extended

[`SCIENTIFIC_VALIDATION.md`](../../SCIENTIFIC_VALIDATION.md) records scoped
isotope/adduct analytical validation, partial mzML ingestion and similarity
validation, and NOT VALIDATED significance. Its prose includes older statements
about what was “wired in”; the capability-specific evidence and status table
are the relevant claim boundaries.
[`VALIDATION_SIMILARITY.md`](../../VALIDATION_SIMILARITY.md) documents one bounded
MassBank retrieval experiment and a consequential greedy-rule discrepancy.
No experiment in this documentation change repairs that discrepancy, validates
library-search error control, or confirms any structure in the acquisition.

The original provider/server tool descriptions contain older MSP-only wording;
actual MGF provider behavior is verified by existing tests. This documentation
task does not modify wire descriptions or public tool behavior.

## Publication boundary

The experimental dataset's CC BY 4.0 attribution and the separate library
record's individual dl-de/by-2-0 attribution are retained in the
[demonstration record](README.md). Do not infer individual record rights from
an aggregate library's licence: the first MassBank entry inspected had a
different licence. No binary datasets, private paths, credentials or GUI mockups
are included. The reproducible replay is the agent deliverable; capture at a real client is pending on Eric.

## Hero-example revision

A question and source-order selection rule were recorded before replacement
scan inspection in [SCAN-SELECTION.md](SCAN-SELECTION.md). The selected later
MS/MS scan rejects the fixed tryptophan hypothesis; no post-result formula
substitution was made. [CAPTURE.md](CAPTURE.md) carries the updated numeric
values and privacy requirements. Prior-head successful CI is cited in
[RESULTS.md](RESULTS.md). Capture is a human presentation task.
