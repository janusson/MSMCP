# Experimental MCP demonstration — 2026-10-20

**Status: verified computation and MCP replay; screenshot INCOMPLETE.** This is
a demonstration of traceable mass-consistency checking, not compound
identification, a performance benchmark, or end-to-end scientific validation.
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
uv run python examples/public_demo.py prepare --data-dir /tmp/msmcp-demo-data
```

```bash
# Spawn this checkout's actual server and save a new, independently checked run.
# Choose a different output directory for each run; existing runs are preserved.
uv run python examples/public_demo.py replay --data-dir /tmp/msmcp-demo-data \
  --output /tmp/msmcp-demo-run
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

The main workflow calls these tools on a new server:

1. `load_mzml_summary(file_path="32Mix_Phala_Tyr_1ugml_1.mzML", max_spectra=2)`.
2. `load_spectrum(..., spectrum_index=8)` followed by `summarise_reference`
   using the returned reference. Index 8 is the first MS/MS spectrum, native
   ID `controllerType=0 controllerNumber=1 scan=9`.
3. `annotate_isotopes(identifier="C6H18O3Si3", isotopologues=3)` and
   `predict_adduct_offset(adduct_string="[M+H]+")`.
4. `validate_precursor(theoretical_mass=223.0636505093789,
   experimental_mass=223.063531641103, instrument_class="orbitrap")`.
5. Release the reference after inspection.

Recorded values from `load_spectrum`:

| Observation | Value |
|---|---|
| MS level / polarity from XML | MS2 / positive |
| Retention time | 0.041191289 min |
| Selected-ion m/z | 223.063531641103 |
| Stored peaks | 51; 816 bytes |
| Sum of stored intensities (TIC) | 14,912,669.678710938 |
| Strongest MS/MS peak (tool text) | m/z 225.0424 |
| Provenance | `load_spectrum`, `msmcp.mzml`, file SHA-256 and software versions |

**LLM proposal:** in the real Codex interaction during this task, the assistant
proposed testing protonated C6H18O3Si3 after observing the precursor. This was
exploratory, not a blinded identity test or a prespecified validation experiment.
The connected MSMCP tools returned the summary, stored-spectrum metadata,
isotope/adduct text and precursor verdict. That already-running server's source
revision was not independently bound to the new branch; the separate recorded
stdio replay binds the same numeric result to the documented checkout.

**Tool calculation:** `validate_precursor` reports **VALIDATION PASSED**,
theoretical 223.063651, observed 223.063532 and **0.53 ppm**, with the
Orbitrap class default **5.0 ppm**. Its error is absolute. This verdict tests
an arithmetic window, not analytical-method validation or instrument calibration.

**Evidence established:** the selected precursor is mass-consistent with this
formula/adduct assumption. C6H18O3Si3 is compatible with the formula of
[hexamethylcyclotrisiloxane](https://pubchem.ncbi.nlm.nih.gov/compound/10914),
but the observation does not identify that structure. No isotope-envelope fit,
isolation-purity assessment, fragmentation assignment, matched reference standard,
blank comparison or retention-time confirmation was performed. The strongest
MS/MS peak lies above the selected precursor; the simple formula test does not
explain the spectrum. No background-source or contamination claim is justified.

## Independent numerical calculation

The script's decoder uses only standard-library XML, base64, zlib and struct;
it does not import MSMCP's parsers, formula parser or physical constants.
It independently counts MS levels and decodes scan 9's original binary arrays.
`math.fsum` reproduces the stored-array TIC and the peak count is 51.

For a singly charged protonated neutral, expected m/z = M + proton mass.
Using 12C = 12 exactly, [1H = 1.00782503223](https://pml.nist.gov/cgi-bin/Compositions/stand_alone.pl?ele=H),
[16O = 15.99491461957](https://pml.nist.gov/cgi-bin/Compositions/stand_alone.pl?ele=O),
[28Si = 27.97692653465](https://pml.nist.gov/cgi-bin/Compositions/stand_alone.pl?ele=Si),
and [CODATA 2022 proton mass = 1.0072764665789 u](https://physics.nist.gov/cgi-bin/cuu/Value?mpu%7Csearch_for=proton+mass):

```text
M = 6×12 + 18×1.00782503223 + 3×15.99491461957 + 3×27.97692653465
  = 222.056374042800 Da
expected [M+H]+ m/z = 223.0636505093789
signed ppm = (observed - expected) / expected × 10^6
           = -0.5328894942 ppm
5 ppm half-window = 0.001115318253 Da
```

The tool's formula/adduct text rounds to four/six decimal places; do not add
those rounded strings and pretend they preserve the independent precision.
The independent full-precision ion value is supplied explicitly to the precursor
tool. This checks the mass-error arithmetic and verdict; it does not independently
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

## Real-client reproduction and screenshot specification

**Screenshot deliverable: INCOMPLETE.** The computer-use API refused access to
the actual Codex app (`com.openai.codex`) for safety reasons. No alternate capture
mechanism was used to bypass that restriction. Zed was open on unrelated work;
no screenshot of that work was taken, no fabricated conversation was composed,
and no screenshot file is claimed to exist.

To obtain a real capture locally, use the actual branch and prepared data above.
In Zed, open Settings → AI → MCP Servers → Add Server → Add Local Server (see
[Zed's documentation](https://zed.dev/docs/ai/mcp)). Use an absolute checkout path
and this configuration, replacing the two placeholders locally:

```json
{
  "context_servers": {
    "msmcp-public-demo": {
      "command": "uv",
      "args": ["run", "--directory", "<ABSOLUTE_CHECKOUT>", "msmcp"],
      "env": {
        "MSMCP_ALLOWED_ROOT": "/tmp/msmcp-demo-data",
        "MSMCP_MAX_SPECTRA": "5000"
      }
    }
  }
}
```

Confirm the server is active. In a fresh Zed Agent conversation with the user's
existing model connection, send this **prompt for a new real interaction**:

> Use MSMCP to summarize `/tmp/msmcp-demo-data/32Mix_Phala_Tyr_1ugml_1.mzML`
> (two spectra), load spectrum index 8 and summarize its returned reference.
> Is this precursor consistent with protonated C6H18O3Si3? Call the formula
> and adduct tools. Independently calculated [M+H]+ m/z is 223.0636505093789
> from the cited NIST/CODATA constants. Use the observed full-precision
> precursor and `validate_precursor` with `instrument_class="orbitrap"`.
> Show observed/expected m/z, ppm error, 5 ppm window, source hash, and a
> concise explanation that passing mass consistency does not identify a
> structure. Do not perform a library search or infer a contamination source.

Approve the tool calls in the real client. Check them against the recorded run;
do not paste a prewritten assistant answer into the conversation. The LLM's
wording can differ; its numeric claims must agree with its actual tool results.

Capture one window at approximately 1600×900 with the prompt, expanded real
`validate_precursor` result and the model's interpretation visible together.
Use readable text, hide unrelated sidebars/tabs and keep paths under `/tmp`.
Include the provenance hash or a shortened hash visibly linked to this record.
Inspect the image at desktop size and a 390-pixel-wide preview before acceptance.
Save the actual PNG as `docs/assets/msmcp-real-mzml-precursor-check.png` only
after verifying no user paths, private files or credentials appear. A crop for
framing is acceptable if disclosed; never replace, redraw or splice tool results.

**Caption draft:** A real acquisition, a chemical hypothesis, and a traceable
calculation. MSMCP reads scan 9 and checks a protonated-formula hypothesis:
0.53 ppm mass error within a 5 ppm window. This establishes mass consistency;
it does not identify the compound. Data: G. A. Vitale, Zenodo
10.5281/zenodo.10043877, CC BY 4.0; derived summaries and calculations.

**Alt text draft — use only after the capture matches:** Real AI-client
conversation showing an MSMCP mzML spectrum inspection and precursor check:
observed m/z 223.063532, expected 223.063651, absolute error 0.53 ppm,
5 ppm Orbitrap profile, and a statement that mass agreement does not establish
structural identity.

## Readiness

**NOT READY for the requested screenshot-led LinkedIn announcement.** The
experimental arithmetic demonstration and supported-format replay are available;
the authentic screenshot still requires local capture and visual verification.
Similarity validation remains partial and wrong-compound significance/FDR remains
unvalidated. No release, merge, social post, identification claim or deployment is
implied. See [verification results](RESULTS.md) for the checks actually run.
