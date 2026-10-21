# Experimental MCP demonstration — 2026-10-20

**State: verified computation and reproducible stdio replay; capture pending on Eric.**
The replay is the agent deliverable. A human at a real MCP client performs the
capture using the [numbered runbook](CAPTURE.md). This is a demonstration of
traceable hypothesis rejection, not compound identification, a performance
benchmark, or end-to-end scientific validation.
No scientific algorithms were changed to obtain these results.

## Public acquisition and attribution

Giovanni Andrea Vitale (University of Tübingen), *ChemM_AQC_32mix_raw and mzml*
(2023), [Zenodo DOI 10.5281/zenodo.10043877](https://doi.org/10.5281/zenodo.10043877),
[CC BY 4.0](https://creativecommons.org/licenses/by/4.0/).
The record and licence were checked through the Zenodo API on 2026-10-20.
The original acquisition is unchanged; this demonstration derives summaries
and calculations. Attribution does not imply the depositor's endorsement.

| Identity | Value |
|---|---|
| File | `32Mix_Phala_Tyr_1ugml_1.mzML` |
| Bytes | 40,179,684 |
| Deposit MD5 | `49f99a6bf111b45c6dabdba12867f118` |
| SHA-256 | `7536385fb4ba7203aa39cc9874efcb3e0c4d7e92e2dec9b3958b7ddaba7d66c4` |
| Instrument | Q Exactive HF; electrospray; positive-mode spectra |
| Conversion | ProteoWizard 3.0.23076; mzML records Thermo/Xcalibur peak picking for MS1 and MS2 |
| Acquisition observed | 4,747 spectra: 1,026 MS1 and 3,721 MS2 |

The instrument, polarity and conversion information comes from the acquisition
XML; the deposit description states treated and untreated mixtures. The filename
labels this injection `1ugml`. This demonstration does not independently verify
concentration, composition, recovery, a chromatographic method, or sample identity.
It does not publish the embedded vendor source-file path or instrument serial.

## Reproduce the computation

From a checkout of this branch, with `uv` installed:

```bash
# Install the locked environment in this checkout.
uv sync --locked --extra dev
```

```bash
# Fetch the unchanged public mzML and a small measured MassBank record.
# Existing files must match the pinned hashes; differing files are refused.
uv run python examples/public_demo.py prepare --data-dir /tmp/msmcp-public-demo
```

```bash
# Spawn this checkout's actual server and save a new, independently checked run.
# Choose a different output directory for each run; existing runs are preserved.
uv run python examples/public_demo.py replay --data-dir /tmp/msmcp-public-demo \
  --output "$(mktemp -d /tmp/msmcp-capture.XXXXXX)/run"
```

The replay uses the installed MCP SDK over stdio, with the data directory as
both working directory and `MSMCP_ALLOWED_ROOT`. It sets a 500 MiB file limit
and a 5,000-spectrum ceiling (the default 1,000 would not cover this acquisition).
No model weights, remote AI API, or deterministic embedding mocks are needed.
Once inputs exist, the replay uses local files only. The script generates no
LLM dialogue. It saves actual tool results, initialization, configuration,
script/lock hashes and Git revision in `transcript.json`, and server diagnostics
in `server.stderr.txt`. Only absolute local data/repository roots are replaced
by documented placeholders before saving those files; numeric outputs, tool
text, IDs and timestamps are retained.

The checked-in [recorded run](2026-10-20-transcript.json) is a snapshot, not
the expected output of every future revision. Opaque references and job IDs
change between runs. The script also checks the precursor, peak count, TIC,
file digest and pass/fail verdict against an independent XML/binary decoder
and Decimal arithmetic. The original acquisition and generated input files
stay outside Git.

## Actual acquisition workflow and evidence

The [prospective selection record](SCAN-SELECTION.md) fixes both the question
and the scan-selection rule before replacement-candidate inspection. The first
MS2 spectrum at or after 2.0 min with at least ten peaks and a base peak at
least 0.02 m/z below its precursor is selected. No candidate mass-error result
is used in selection. The fixed question is: **Does protonated tryptophan,
C11H12N2O2 [M+H]+, explain this later MS/MS precursor at 5.0 ppm?**

The main workflow calls these tools on a new server:

1. `load_mzml_summary(file_path="32Mix_Phala_Tyr_1ugml_1.mzML", max_spectra=2)`.
2. `load_spectrum(..., spectrum_index=590)` followed by `summarise_reference`
   using the returned reference. Zero-based index 590 is native scan 591.
3. `annotate_isotopes(identifier="C11H12N2O2", isotopologues=3)` and
   `predict_adduct_offset(adduct_string="[M+H]+")`.
4. `validate_precursor(theoretical_mass=205.0971541013389,
   experimental_mass=724.722412109375, instrument_class="orbitrap")`.
5. Release the reference after inspection (in the replay; capture it before
   releasing in the interactive client).

Recorded values from `load_spectrum`:

| Observation | Value |
|---|---|
| Native scan / zero-based index | 591 / 590 |
| MS level / polarity from XML | MS2 / positive |
| Retention time | 2.0017699 min |
| Selected-ion m/z | 724.722412109375 |
| Stored peaks | 18; 288 bytes |
| Sum of stored intensities (TIC) | 58,098.39074707031 |
| Strongest MS/MS peak | m/z 100.1124267578125 |
| Provenance | `load_spectrum`, `msmcp.mzml`, file SHA-256 and software versions |

**Proposed hypothesis:** protonated tryptophan was fixed before evaluating the
replacement scan. It is a candidate test, not an annotation supplied by the
deposit. The [actual replay](2026-10-20-transcript.json) is an MCP transport
record, not an invented LLM conversation. The capture runbook starts a new
human-driven interaction and makes no claim that its assistant wording already
exists.

**Tool calculation:** `validate_precursor` reports **VALIDATION REJECTED**,
expected **205.097154**, observed **724.722412**, absolute error
**2533556.65 ppm**, Orbitrap profile window **5.0 ppm**. The independent signed
error is **+2533556.65 ppm**; the positive sign belongs in the assistant's
interpretation because the existing tool returns only absolute error.
This is a large mismatch, not a near-threshold accuracy example.

**Evidence established:** this selected precursor is inconsistent with singly
protonated C11H12N2O2 under the stated assumption. Nothing identifies the selected
ion, determines its actual charge/adduct, or establishes whether tryptophan is
present elsewhere in the run. No isolation-purity assessment, diagnostic-fragment
assignment, isotope-envelope fit, reference-standard comparison or retention-time
confirmation was performed. The profile window is a documented default, not
empirical calibration of this acquisition.

**Why the hero changed:** the earlier near-void scan 9 had a siloxane-formula
mass match and its strongest MS/MS peak above the selected precursor. That
combination made an ambiguous public example. The independent selection rule
moves the demonstration later in the run with a dominant lower-mass product ion;
the rejection is retained rather than substituting a formula to obtain a pass.
This selection does not claim statistical representativeness of all spectra.

## Independent numerical calculation

The script's decoder uses only standard-library XML, base64, zlib and struct;
it does not import MSMCP's parsers, formula parser or physical constants.
It independently counts MS levels and decodes scan 591's original binary arrays.
`math.fsum` reproduces the stored-array TIC and the peak count is 18.

For a singly charged protonated neutral, expected m/z = M + proton mass.
Using 12C = 12 exactly, [1H = 1.00782503223](https://pml.nist.gov/cgi-bin/Compositions/stand_alone.pl?ele=H),
[16O = 15.99491461957](https://pml.nist.gov/cgi-bin/Compositions/stand_alone.pl?ele=O),
[14N = 14.00307400443](https://pml.nist.gov/cgi-bin/Compositions/stand_alone.pl?ele=N),
and [CODATA 2022 proton mass = 1.0072764665789 u](https://physics.nist.gov/cgi-bin/cuu/Value?mpu%7Csearch_for=proton+mass):

```text
M = 11×12 + 12×1.00782503223 + 2×14.00307400443 + 2×15.99491461957
  = 204.089877634760 Da
expected [M+H]+ m/z = 205.0971541013389
signed ppm = (observed - expected) / expected × 10^6
           = +2533556.6467747683 ppm
5 ppm half-window = 0.001025485771 Da
```

The tool's formula/adduct text rounds to four/six decimal places; do not add
those rounded strings and pretend they preserve the independent precision.
The independent full-precision ion value is supplied explicitly to the precursor
tool. This checks the mass-error arithmetic and rejection verdict; it does not independently
validate every isotope/adduct path. Source constant rounding and editions make a
negligible difference at this 5 ppm window; no experimental uncertainty was estimated.

## Supported-format library check, kept separate

The script downloads measured record **MSBNK-BAFG-CSL23111015079** from the pinned
[MassBank 2026.03 source](https://raw.githubusercontent.com/MassBank/MassBank-data/2026.03/BAFG/MSBNK-BAFG-CSL23111015079.txt).
Source SHA-256: `bfabf5789fc94d3801755a4a3c47b59200a29334d0334e958ef293e32e2f0089`.
Ole Lessmann, Kevin S. Jewell, Björn Ehlig and Arne Wick; copyright 2025 Federal
Institute of Hydrology, Koblenz, Germany. Licence:
[dl-de/by-2-0](https://www.govdata.de/dl-de/by-2-0). Format conversion is the
declared modification. These terms permit presentation and reuse with the
provider, licence link, source URI and modification notice retained.

The script copies the eight original m/z and intensity tokens into
`favipiravir.msp` and `favipiravir.mgf`; it does not invent or perturb peaks.
The record states a negative-ion LC-ESI-QTOF MS2 measurement on a SCIEX
TripleTOF 6600 at 50 V. For this separate smoke check the MCP client loads that
MGF, searches the MSP using `scoring_method="classical"` and
`instrument_class="tof"`, polls the real job, verifies the real-library banner
and score 1.0000, then releases the reference. The input library has one spectrum.

**This is a self-match.** It verifies supported input formats and actual search
transport. It provides no independent recovery rate and does not identify
anything in the Zenodo acquisition. No synthetic library is invoked. The shipped
search still computes and filters using uncalibrated p/q values; neither those
numbers nor this smoke check establish statistical significance or FDR control.
The one-record library uses the p-value path; its statistics are excluded from
the publication claim. Read [the audit](AUDIT.md) and
[scientific validation](../../SCIENTIFIC_VALIDATION.md) before interpreting search.

## Human capture handoff

**Verified computation and reproducible stdio replay; capture pending on Eric.**
The replay is the completed computational deliverable. Capturing the real client
requires a human at a real MCP client and is Eric's remaining presentation task.
Codex's computer-use policy correctly refused capture of its own interface;
that refusal is not a computational failure or a readiness blocker.

Follow [CAPTURE.md](CAPTURE.md) in order. It supplies the exact preparation,
replay and client-configuration commands, the prompt, required tool sequence,
visible values, pane layout, privacy checks, caption and alt text. The same
`serve` launcher is exercised by the replay and used in the generated Zed config;
it starts this checkout's real server in the data directory so prompts use only
the acquisition basename. Absolute paths can still occur in full tool provenance:
keep `load_spectrum` and `summarise_reference` result bodies collapsed for capture.

## Readiness

**READY FOR ERIC TO CAPTURE.** Computation and replay are verified; capture is
pending on a human. Similarity validation remains partial and wrong-compound
significance/FDR remains unvalidated. Those scientific boundaries remain in the
caption and supporting record; the screenshot is not a new validation experiment.
See [verification results](RESULTS.md), including the successful prior-head CI
check run and the rerun of the current gate.
