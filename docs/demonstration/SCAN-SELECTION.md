# Prospective replacement-scan selection — 2026-10-20

Recorded before examining replacement candidates or computing their hypothesis
mass errors in this revision. This is a demonstration selection rule, not a
scientific validation protocol.

Question: Does protonated tryptophan (C11H12N2O2, [M+H]+, charge +1) explain a
later MS/MS precursor within the Orbitrap profile's 5.0 ppm window?

Select the first source-order MS2 spectrum with retention time >= 2.0 minutes,
at least ten stored peaks, and a base peak at least 0.02 m/z below the selected
precursor. No precursor matching, mass-error threshold, or pass/fail verdict is
used to select it. Keep the selected scan and hypothesis whether the outcome is
a pass or rejection; do not try another formula to improve the presentation.
This moves the demonstration beyond the near-void scan and selects a spectrum
with a lower-mass dominant product ion, without asserting identity or purity.

The question is a candidate test. The deposit does not independently establish
tryptophan content, and no identity, presence/absence for the whole run, or
contamination-source claim will follow from one selected scan.

## Recorded outcome

The rule selected zero-based index 590, native scan 591, RT 2.0017699 min,
18 peaks, base peak m/z 100.1124267578125. The selection was made without
computing candidate mass errors. The fixed hypothesis was then rejected:
observed m/z 724.722412109375 versus expected [M+H]+ 205.0971541013389,
signed +2533556.6467747683 ppm at a 5.0 ppm profile window.

This is a clear negative candidate test, not a near-threshold mass-accuracy
example. It does not establish the selected ion's charge/adduct, identify its
structure, or show that tryptophan is absent elsewhere in the acquisition.
The positive single-charge state is the tested hypothesis, not inferred metadata.
No alternative formula was tried after observing this outcome.
