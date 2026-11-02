# Acquisition-aware QC evidence: bounded signal-distribution slice (#17)

## Integration state and scope

At implementation time, GitHub `main` is `77bf970`. PR #87 is **open and unmerged**; this worktree is deliberately based on PR #87's head (`8a77135`) so the new report builds on its MS/MS applicability and positive-intensity correction without changing that PR. Issue #16 is also **open with no associated PR**. The acquisition-context implementation at local commit `d9723d2` is not on `main` or a remote branch, so this slice neither imports nor assumes that unmerged code.

This is one bounded issue #17 improvement: replace the unsupported SNR/noise-floor interpretation with a per-spectrum description of the intensity values that MSMCP actually receives. It does **not** claim that median or MAD estimates noise.

## Scientific assessment

### Why median + k·MAD is not used as a noise threshold

For a spectrum with recorded intensities `I`, let

`P = { I[j] | isfinite(I[j]) and I[j] > 0 }`.

The report calculates `median(P)` and the unscaled median absolute deviation
`median(abs(P - median(P)))`, only when `P` is non-empty. These are well-defined
statistics of the positive recorded array values. They are **not** a defensible
noise-floor estimate for the inputs MSMCP currently accepts:

- In centroided data, the array is a list of retained features, not a sequence of
  baseline/noise samples. It mixes analyte, chemical background, contaminants,
  and processing/threshold effects.
- Zero, negative, and non-finite values are excluded from the distribution but
  separately counted. This positive-only selection censors the low end; it cannot
  recover how many low signals were removed upstream.
- With one or a few positive centroid peaks, median/MAD describe those peaks. A
  MAD of zero can mean repeated/equal values or one positive observation; it does
  not mean zero noise. Highly skewed signal-plus-interference mixtures likewise
  do not make MAD a noise model.
- Profile arrays may include baseline samples, but their sampling, smoothing,
  vendor processing, and intensity scale are not standardised by the current
  `Spectrum` contract. Applying the same statistic to profile and centroid arrays
  does not give them the same physical interpretation.
- `Spectrum` carries no validated noise-only m/z region, blank, local baseline,
  or instrument-calibrated threshold. A positive peak can be noise; a low peak can
  be real.

Consequently there is **no k parameter and no k·MAD cutoff**. Adding a configurable
k would create an adjustable heuristic, not a validated noise estimate. A future
noise-floor method needs a justified noise-only sampling model and independent
validation on representative acquisition types.

### Metric specification

| Metric | Definition | Units | Prerequisites | Interpretation / failure behaviour |
|---|---|---|---|---|
| Positive finite count | `n = count(isfinite(I) and I > 0)` | count | 1-D intensity array | Number of values included in the descriptive distribution; not number of analyte ions |
| Zero / negative / non-finite counts | Exact counts in the input array | count | 1-D intensity array | Reported separately; excluded from the median and MAD; the original array is not modified |
| Positive median | `median(P)` | Source intensity scale; unit may be unspecified | `n > 0` | Describes retained positive values only; absent (`N/A`) when `n = 0` |
| Unscaled MAD | `median(abs(P - median(P)))` | Same source intensity scale | `n > 0` | Descriptive spread only; no 1.4826 factor, k multiplier, or noise meaning is applied |
| Applicability | `descriptive_only` if `n > 0`; otherwise `not_applicable_no_positive_values` | categorical | Always | No fallback numeric zero for an empty/zero-only/non-finite spectrum |

MSMCP does not rescale these values; any source-side scaling or normalization is
unknown. No statistic is normalised across spectra or files. The report displays at most 20
per-spectrum rows in reader order and states how many rows were omitted; metrics
are still calculated for every spectrum in the processed subset. The only
selection parameter is the fixed, reported finite-positive rule. The existing
`MSMCP_MAX_SPECTRA` limit controls the processed subset and is disclosed by QC
coverage reporting. The instrument class applies to the diagnostic-ion m/z window
only; its legacy `snr_low` / `snr_high` fields are retained for compatibility but
are no longer used as QC noise or quality cutoffs.

### Other evidence currently available

- **Observed feature:** PR #87 checks positive finite intensity and mass entries against
ten theoretical immonium/oxonium masses with an explicit absolute-Da window. The
instrument-profile window is reported as applied; it is not a ppm-relative error
bound and has not been validated here against an instrument-specific fragment
calibration. MS1
is excluded when MS/MS is available; unknown MS levels are disclosed and used only
when no declared MS/MS spectra are available. This is a presence/prevalence
heuristic. A weak positive match is still counted as an observed mass coincidence;
this report does not establish that it exceeds noise.
- **Isolation and collision context:** an isolation-window width, where present,
  describes a selection range, not the abundance of ions within that range. The
  current integration branch has no validated co-isolated-ion abundance model,
  no linked MS1 purity calculation, and no integrated acquisition context for
  dissociation method/activation source. MS level alone cannot attribute a feature
  to a particular collision process. Purity is therefore not calculated.
- **Chemical interpretation:** no identity, sequence, glycan structure, calibrated
  probability, or false-discovery rate follows from these summaries.

## Deterministic worked examples

These examples show what the metric says, not whether a spectrum is analytically
"good" or "poor". The raw values have arbitrary source-dependent scale.

| Case | Input intensities | Expected reported distribution | What can be said / cannot be said |
|---|---|---|---|
| Strong-looking recorded values | `[75, 80, 90, 100]` | `n=4`, median `85`, unscaled MAD `7.5` | Four positive values with this spread were recorded. Without a background model, this is not a high-SNR or high-confidence result. |
| Sparse, zero-censored values | `[0, 0, 0, 0.02]` | `n=1/4`, zero count `3`, median `0.02`, MAD `0` | One small positive value and three zeros were recorded. Whether the positive value is noise or analyte is indeterminate. |
| Skewed/repeated values | `[2, 2, 2, 1000]` | `n=4`, median `2`, unscaled MAD `0` | MAD zero does not imply a noiseless spectrum; the high value may be analyte, interference, or artifact. |
| Baseline/noise versus weak fragments | `[0.1, 0.2, 0.3, 100]` | `n=4`, median `0.25`, unscaled MAD `0.1` | The same observed values could be three background features plus an analyte, or three real low-abundance fragments plus one major fragment. The calculation cannot tell which interpretation is true. |
| No usable positive values | `[NaN, +Inf, -Inf, -1, 0]` | `n=0/5`, zero `1`, negative `1`, non-finite `3`; median/MAD `N/A` | The distribution metric is not applicable. It does not report a zero noise floor or reassuring score. |
| Weak diagnostic coincidence | m/z `136.0757`, intensity `1e-300` | Positive finite mass coincidence is present in the PR #87 mask | The feature is observed but has no validated noise support. No chemical assignment follows. |

The deterministic tests assert these calculations and edge cases. They validate
implementation of the definitions, not the physical validity of a noise model;
there is deliberately no such model in this slice. No representative acquired
fixture was available, so no throughput benchmark or claim is made. The metric
uses O(P) temporary arrays per spectrum (where P is its peak/value count) and
retains one small scalar summary per processed spectrum; processing remains
bounded by the existing spectrum limit and report rows by the 20-row cap.

## Report language and provenance

`generate_qc_summary` now reports **Observed Peak-Intensity Distribution** with
per-spectrum median/MAD, counts, and explicit applicability. The former
"Signal-to-Noise Ratio" section is removed because its denominator (median positive
recorded intensity) is not a measured noise sample. Diagnostic mass matches are
labelled observed features, and an evidence-limits section states that there is no
noise-floor, isolation-purity, identification-confidence, probability, or FDR
calculation.

The report itself records the metric provenance: source format/backend, processed
coverage, positive-finite selection rule, median and unscaled MAD definitions,
row cap, diagnostic absolute-Da window, and the instrument class. No raw spectral
values or arrays are changed.

## Coverage matrix

| Requirement | Status in this slice | Evidence / limitation |
|---|---|---|
| Separate observed m/z features from signal statistics | Implemented | Separate diagnostic section and per-spectrum intensity-distribution section |
| Numerical per-spectrum signal summary | Implemented, descriptive only | Median + unscaled MAD of finite positive values; zero/negative/non-finite counts; `N/A` if none |
| Defensible per-spectrum noise floor / k·MAD threshold | Not implemented | Current arrays do not identify background-only samples; do not interpret these statistics as noise |
| Diagnostic m/z accuracy | Existing absolute-Da presence matching retained | Applied tolerance is stated; no ppm-relative/validated fragment-error calibration added here |
| Noise-supported fragment evidence / relative intensity criterion | Not implemented | Positive intensity alone can be noise; no validated cutoff or blank-derived distribution |
| MS-level applicability | Implemented by PR #87, which remains an unmerged base dependency | MS1 excluded where MS/MS is available; unknown-level fallback disclosed |
| Isolation purity / co-isolation risk estimate | Not implemented | A window width is not abundance or purity; requires MS1 precursor/isotope data and validated model |
| Instrument acquisition metadata from issue #16 | Not integrated | Issue #16 remains open; its code is local-only and is not used by this branch |
| Compound confidence, probability, calibrated FDR | Not implemented | No independent labelled calibration/evaluation set or prespecified error-rate validation |

## Follow-up required to close issue #17

1. Obtain representative raw/profile and centroid data, blanks, and independently
   identified signal/noise examples across instrument classes and processing
   pipelines. Prespecify a noise model and validate calibration and failure cases
   on held-out batches before naming any quantity a noise floor or SNR.
2. Integrate issue #16 only after review/merge. Resolve precursor/MS1 relationships
   and actual asymmetric isolation settings, then design an isolation-purity
   estimator from observed co-isolated-ion abundance. Validate it against an
   appropriate reference, not against the estimator's own output.
3. Validate fragment mass windows and any intensity/noise support criterion using
   blank/interference controls and independent MS/MS annotations. Keep observed
   presence separate from support and from identity.
4. Do not introduce confidence probabilities or FDR claims without independent
   calibration and evaluation compounds/batches, a defined testing family, and
   prespecified acceptance criteria.
