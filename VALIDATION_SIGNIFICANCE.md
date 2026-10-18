# Search significance: uncalibrated

Production search returns library candidates ordered by similarity. It does not
report p-values or q-values, apply a significance cutoff, or claim false-discovery
rate control. This applies to every library size, all supported scoring methods,
and real and synthetic libraries. The top 20 candidates are shown even when all
similarities are zero; their presence is not evidence of a compound identification.

Full reports and repeat-poll summaries explicitly say **SIGNIFICANCE UNCALIBRATED**.
Provenance identifies contract `search_result_contract="msmcp.search.similarity.v1"`
and records `significance_status="uncalibrated"`,
`significance_threshold=null`, `multiple_testing_method=null`,
`fdr_control_validated=false`, and `significance_filter_applied=false`. The summary's
`threshold` and `top_significance` are null. `n_hits` now counts ranked library
candidates, including candidates outside the displayed top 20, rather than
statistically significant discoveries. Consumers of the old report table must
adapt to its removal of the significance column.

The profile fields `fdr_threshold` and `small_library_threshold` remain recorded
as legacy settings for compatibility. They do not select a production search mode.
The decoy and tail-fitting utilities remain available for offline investigation;
production search neither samples decoys nor fits their upper tail. No calibration
file or experimental flag can silently restore the former significance claims.

## Why significance was withdrawn

The former search fitted a generalized Pareto tail to intensity-permuted library
spectra, extrapolated p-values down to a numerical floor, and applied BH in large
libraries. Small libraries instead used unadjusted empirical decoy p-values.
Neither path established that real wrong-compound target scores had admissible
null p-values. Fitting the decoy distribution, increasing library size, or making
software tests pass does not establish that condition. Returning p=1 for an empty
decoy sample is not calibration either.

Target–decoy error control depends on assumptions about false target and decoy
scores; failures of those assumptions can invalidate the estimate. See
[Gupta et al., 2011](https://doi.org/10.1007/s13361-011-0139-3) and the peptide-library
experiments of [Zhang et al., 2018](https://doi.org/10.1021/acs.jproteome.7b00614).
Those peptide results do not validate this small-molecule null.
[BH](https://doi.org/10.1111/j.2517-6161.1995.tb02031.x) also requires valid null
p-values and appropriate dependence assumptions for the defined testing family.

## Evidence required to restore an error-rate claim

A replacement should calibrate against independent, identity-confirmed queries
and genuinely wrong target compounds in the intended acquisition domain. Existing
library identities may supply reference labels, but independent query acquisitions
and checks for label errors remain necessary. Calibration and evaluation must be
separated by compound and acquisition batch. All reference spectra of a compound
must be kept together; a spectrum-level split can leak compound identity.

Before running the evaluation, freeze library and input digests, acquisition
strata, compound identity rules, search gates, candidate selection, score
calibration, testing family, seeds, sample size and acceptance criteria. Record
every result, including failures, and uncertainty based on independent experimental
units. A sample of thousands of correlated target pairs is not thousands of
independent observations.

Check null p-value super-uniformity on preselected known-wrong target hypotheses.
An incorrect top-ranked target is selected by the search; its raw p-value need
not be uniform even when individual target p-values are valid. Assess selection
and multiplicity using the complete reported list and independently known truth.
Batch FDR is the expectation of each batch's false discovery proportion, with
zero assigned to batches without discoveries. It is not generally the pooled
ratio of false hits to hits, nor an individual hit's posterior error probability.

No suitable labelled evaluation has been run for this change. Its regression
tests establish the software's conservative reporting behavior only. A validated
wrong-compound null and a measured 5% FDR remain outstanding scientific work.
