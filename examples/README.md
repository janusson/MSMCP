# MSMCP Examples

This directory contains three kinds of demonstration material:

1. **Synthetic caffeine fixtures** (`caffeine_spectrum.mgf`, `caffeine_library.msp`) — committed, fabricated demonstration spectra used by the README quickstart and automated tests to exercise MS/MS search mechanics deterministically.
2. **Offline reproducible workflows** ([`reproducible_workflows.py`](reproducible_workflows.py)) — runs three deterministic, network-free workflows over the real MCP stdio transport without installing optional extras or downloading datasets.
3. **Real MassBank library search** ([`workflow/`](workflow/README.md)) — downloads the pinned MassBank `2026.03` release (`SHA-256` verified) to run an end-to-end search against measured MS2 spectra.

All workflows in `reproducible_workflows.py` are **software mechanics demonstrations**, not analytical validation. See [`../SCIENTIFIC_VALIDATION.md`](../SCIENTIFIC_VALIDATION.md) for what is and is not independently validated.

---

## Requirements and compatibility

- **Python:** `>= 3.13`
- **Package compatibility:** **`msmcp>=1.0.2,<2`** (reproduced and verified against `1.0.2` using core dependencies only — no optional `imaging`, `chem`, `ml`, or plotting dependencies are required).

Run from a source checkout:

```bash
uv run python examples/reproducible_workflows.py
```

Or against an installed `msmcp` wheel (for example `pip install "msmcp>=1.0.2,<2"`):

```bash
python examples/reproducible_workflows.py --server msmcp
```

By default, `reproducible_workflows.py` copies `caffeine_spectrum.mgf` and `caffeine_library.msp` into a fresh temporary directory, writes the synthetic `mzML` fixture there, confines `MSMCP_ALLOWED_ROOT` to that temporary directory, drives `tools/list` (all 15 tools) and all three workflows over stdio, and cleans up automatically. Pass `--work-dir` or `--json-out` if you want to inspect the generated `synthetic_lcms.mzML` or raw JSON outputs locally.

---

## Workflow 1: Exact-mass chemistry & precursor gate

Reproduces exact-mass calculations for caffeine (`C8H10N4O2`):

1. `annotate_isotopes(identifier="C8H10N4O2", isotopologues=4)` returns monoisotopic neutral mass **194.0804 Da** and relative abundances normalized to `M = 1.0000`: `1.0000`, `0.1032` (`M+1` at `195.0829 Da`), `0.0089` (`M+2` at `196.0850 Da`), `0.0006` (`M+3` at `197.0872 Da`).
2. `predict_adduct_offset(adduct_string="[M+H]+")` returns polarity `positive`, charge state `+1`, and exact mass shift `+1.007276 Da`. Combining the two displayed rounded values (`194.0804 + 1.007276`) gives `195.087676 m/z`.
3. `validate_precursor(..., instrument_class="orbitrap")` compares two illustrative inputs against the Orbitrap preset (`±5.0 ppm`):
   - `195.087700` → `+0.12 ppm` (**VALIDATION PASSED**)
   - `195.090000` → `+11.91 ppm` (**VALIDATION REJECTED**)

**Limitations:** `±5.0 ppm` is a scenario preset convention rather than instrument calibration. The isotope calculation models a neutral theoretical formula without comparing an observed envelope, and a precursor pass never identifies a compound.

---

## Workflow 2: Synthetic MS/MS library search & cosine similarity

Reuses the existing fixtures `examples/caffeine_spectrum.mgf` (1 MS2 spectrum, 4 peaks) and `examples/caffeine_library.msp` (5 MSP entries, where the Caffeine entry duplicates the query's 4 peaks):

1. `load_spectrum(file_path=..., spectrum_index=0)` registers a `ptr:spectrum:…` handle server-side (`ms_level: 2`, `precursor_mz: 195.0877`, `4` peaks).
2. `search_library(..., scoring_method="classical", instrument_class="generic", chunk_size=100)` dispatches a background scan; polling `check_search_status(..., full_report=True)` returns **Caffeine** at rank 1 with cosine **1.0000**, excluding the other 4 entries outside the `±5.0 ppm` precursor window.
3. `compute_cosine` on the 4-peak lists returns `1.0000` (`4 / 4 query peaks` matched, `0` unmatched under `±0.020 Da` greedy matching), followed by `release_reference`.

**Context gate and scientific caveats:**

- In `search_library`, `polarity`, `charge`, and `adduct` are evaluated only for fields declared by the **query**. Because `caffeine_spectrum.mgf` provides `MSLEVEL=2` and `PEPMASS=195.0877` while omitting polarity, charge, and adduct, `search_library` reports `Context eligibility: 1 matched, 0 unknown, 4 excluded` and marks Caffeine `Context: matched` even though `caffeine_library.msp` specifies `Ion_mode: P` and neither record specifies charge or adduct. Accordingly, **`Context: matched` in this output confirms precursor tolerance compatibility and no query-declared context conflicts — it does not establish complete polarity, charge, or adduct verification.** (When a query *does* declare polarity, charge, or adduct and a library entry omits them, that candidate remains ranked and is labelled `unknown`.)
- Because the Caffeine MSP record intentionally duplicates the query peaks, the `1.0000` score exercises search mechanics only; production reports explicitly state `SIGNIFICANCE UNCALIBRATED` (no p-values, q-values, or FDR control).

---

## Workflow 3: Synthetic LC–MS exploration & QC summary

At runtime, `reproducible_workflows.py` builds a deterministic centroid `mzML` fixture (`synthetic_lcms.mzML`: 41 positive MS1 scans at `0.00–4.00 min` containing constructed Gaussian signals centered around `195.0877` at `1.5 min` and `250.1234` at `2.7 min`, plus 4 interleaved MS2 scans):

1. `list_spectra(file_path=..., max_spectra=100)` enumerates all `45 of 45` spectra with zero-based indices (`41` MS1, `4` MS2).
2. `extract_chromatograms(file_path=..., targets_mz=[195.0877, 250.1234], tolerance=5.0, tolerance_unit="ppm", ms_level=1, max_spectra=10000)` returns structured scan-level TIC, BPC, and inclusive `±5.0 ppm` XIC traces (`schema_version: "1.0"`, `spectrum_counts_by_ms_level: {"1": 41, "2": 4}`, `full_acquisition_processed: true`, `41` retention-time points from `0.0` to `4.0 min`).
3. `generate_qc_summary(file_path=..., instrument_class="generic")` reports `45` spectra analyzed (`complete` coverage) and an uncalibrated proxy SNR (`base-peak intensity ÷ median positive intensity`).

**Limitations:** All peaks and intensities in `synthetic_lcms.mzML` are deterministic synthetic Gaussians (no noise model, blanks, standards, smoothing, or chromatographic/quantitative validation).
