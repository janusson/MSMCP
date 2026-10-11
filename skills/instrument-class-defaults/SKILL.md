---
name: instrument-class-defaults
description: Where msmcp keeps experimental thresholds, how instrument classes carry their own defaults and provenance, and why literals must not creep back in.
category: data
version: 1.0.0
---

# Instrument-class defaults

Use this whenever a change touches an experimental threshold (a tolerance, an
SNR band, an FDR gate) or the provenance of one. All such constants live in one
module: `src/msmcp/instruments.py`.

## Why it exists

An audit found the thresholds that encode the experimental question were
module-level literals: a 5.0 ppm precursor gate, a 0.02 Da peak-match window, a
0.02 Da diagnostic-ion window, SNR cut-offs of 5 and 20, and an FDR threshold /
small-library floor of 0.05 / 2000. The consequences were that an analysis
could not be re-run under different instrument assumptions without editing
source, and the value *actually used* was invisible in the result it produced.

`instruments.py` is the single home for those constants.

## The model

- `InstrumentClass` is `Literal["generic", "orbitrap", "tof", "ion_trap",
  "triple_quad"]`.
- `InstrumentProfile` holds one class's documented defaults plus a `provenance`
  string stating the basis for every number.
- `Tolerance` pairs a value with its unit (`"ppm"` or `"Da"`), because a ppm
  window is meaningless on a unit-resolution instrument.
- `get_profile(instrument_class)` resolves a name; `None` yields the default
  class. **An unknown name raises rather than falling back**, so a misspelled
  class cannot silently change an analysis.

## Per-class defaults

| Class | Precursor | MS2 window | Diagnostic-ion window |
|---|---|---|---|
| generic | 5.0 ppm | 0.02 Da | 0.02 Da |
| orbitrap | 5.0 ppm | 0.01 Da | 0.01 Da |
| tof | 15.0 ppm | 0.05 Da | 0.05 Da |
| ion_trap | 0.5 Da | 0.5 Da | 0.5 Da |
| triple_quad | 0.7 Da | 0.7 Da | 0.7 Da |

SNR bands (5 / 20) and the FDR threshold / small-library floor (0.05 / 2000) are
shared across classes today: they are properties of the reporting bands and the
statistics, not of the mass analyser. `generic` reproduces MSMCP's v1.0 values
exactly, so existing analyses stay reproducible.

## The rule

- **Never reintroduce a threshold literal.** Read it from the profile.
- **Values travel into the result that used them.** String-returning tools
  state the value and the class in the report body; `search_library` records the
  whole profile in `Provenance.parameters`.
- **These are documented conventions, not measured acquisition parameters.**
  MSMCP does not yet parse instrument model, resolution, or calibration from a
  file. State the assumption a result was produced under rather than hiding a
  bare number.

## Adding a class or a constant

1. Add or edit the profile in `_PROFILES` with a `provenance` string that says
   where the number comes from.
2. Keep `generic` equal to the v1.0 values unless a change is deliberate and
   called out.
3. If a tool consumes the new constant, thread it through so it reaches the
   report or `Provenance.parameters`.
4. Add tests in `tests/test_instruments.py`, and check any tool that reports the
   value.
