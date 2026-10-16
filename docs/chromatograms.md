# Scan-level chromatograms

`extract_chromatograms` reads one local `.mzML` or `.mzML.gz` file and returns
structured TIC, base-peak chromatogram (BPC), and up to 20 extracted-ion
chromatograms (XICs). All requested traces come from one streaming pass over the
acquisition. The MCP output schema is published with the tool and the result is
available as `structuredContent`; clients should consume those fields directly
rather than parse the text content.

## Example call

Call once for the sample and once for the blank, using the same investigation
parameters:

```json
{
  "file_path": "/data/32Mix_Phala_Tyr_1ugml_1.mzML",
  "targets_mz": [223.0634, 297.0822, 299.0614, 281.0508],
  "tolerance": 5.0,
  "tolerance_unit": "ppm",
  "ms_level": 1,
  "max_spectra": 5000
}
```

Use the same request with the blank path. The 5 ppm window is an investigation
parameter only; it is not a validated identification threshold.

An optional inclusive retention-time filter is expressed in minutes:

```json
{
  "file_path": "/data/run.mzML",
  "targets_mz": [223.0634],
  "tolerance": 0.01,
  "tolerance_unit": "Da",
  "ms_level": 1,
  "retention_time_range_min": [1.0, 3.0],
  "max_spectra": 5000
}
```

## Response schema

The wire `outputSchema` is generated from `ChromatogramToolResult` in
`src/msmcp/tools/chromatograms.py`. `structuredContent` has this stable shape:

| Field | Type and meaning |
|---|---|
| `schema_version` | String; currently `"1.0"`. |
| `source` | Object with `file_path`, `format`, `reader_backend`, `size_bytes`, and `digest` (`sha256:<hex>` or null). |
| `acquisition` | Object with `scanned_spectra`, `spectrum_counts_by_ms_level` (string keys, including `"unknown"`), `selected_ms_level`, `selected_ms_level_spectra`, `retention_time_coverage_min` (`start_min`/`end_min`, over the full source), `retention_time_units`, `representation_counts`, `selected_representation_counts`, `polarity_counts`, `selected_polarity_counts`, `empty_peak_array_spectra`, `selected_empty_peak_array_spectra`, and `missing_retention_time_spectra`. |
| `chromatograms` | Object with parallel arrays `scan_indices`, `scan_ordinals`, `source_spectrum_indices`, `retention_times_min`, `tic_intensities`, and `bpc_intensities`; plus `xic`, `mz_units`, `retention_time_units`, and `intensity_units`. Each XIC has `target_mz`, inclusive `lower_mz_inclusive`/`upper_mz_inclusive`, `aggregation`, `intensity_units`, and its `intensities` array. |
| `extraction_parameters` | Object with `targets_mz`, `tolerance`, `tolerance_unit`, `xic_aggregation`, `tic_calculation`, `bpc_calculation`, `ms_level`, `retention_time_range_min`, `max_spectra`, and `resource_limit_counts`. |
| `completeness` | Object with `full_acquisition_processed`, `scanned_spectra`, `max_spectra`, `resource_limit_counts`, and `returned_points`. |
| `missing_observations` | Array of objects with `scan_ordinal`, nullable `source_spectrum_index`, nullable `ms_level`, nullable `retention_time_min`, and `reason`. |
| `warnings` | Array of strings describing missing/unknown metadata and interpretation limits. |
| `provenance` | Structured MSMCP provenance, including operation, actual parameters, source identity/digest where available, and software versions. |

All trace arrays have the same length. `scan_ordinals` are zero-based positions
in reader order. `scan_indices` use the source spectrum index when present and
fall back to the ordinal; `source_spectrum_indices` preserves a null when the
file has no index. Scan order and duplicate or irregular retention times are
preserved. When an RT filter is supplied, points outside it are omitted, but
the tool still scans the full acquisition for counts, coverage and completeness.

## Calculation and missing-data rules

- TIC is the float64 sum of every reported intensity in each selected MS-level
  scan. It does not depend on the XIC targets.
- BPC is the maximum reported intensity in each selected scan. It is null for a
  valid empty scan because an empty scan has no base peak.
- Each XIC uses `target_mz ± tolerance`: ppm half-width is
  `target_mz * tolerance * 1e-6`; Da half-width is the tolerance itself. Both
  boundaries are inclusive. The value is the float64 sum of all reported scan
  intensities whose m/z is inside that window.
- A valid empty scan has TIC 0 and XIC 0; its BPC is null. A scan with a missing
  or invalid retention time or non-finite/mismatched peak arrays has null trace
  intensities and a `missing_observations` entry. A missing RT cannot be tested
  against an RT filter, so that scan is listed as missing and omitted from the
  filtered trace. The tool does not fabricate a zero for an unavailable value.
- mzML centroid/profile terms are reported when present. For centroided scans,
  XIC sums the intensities of all centroids inside the inclusive window. For
  profile scans it sums sampled profile intensities in that window and warns
  that the result is not a centroid-peak sum. When representation is unknown,
  the same sum over reported points is returned with an explicit warning; it
  is not labeled centroided.
- Retention times are normalized to minutes by the mzML reader. Intensities are
  returned in input-defined units; this reader does not infer or convert an
  instrument intensity unit. Absolute TIC/XIC/BPC comparisons between files
  can be affected by acquisition settings, gain, detector response and export
  processing.
- No smoothing, interpolation, peak detection, alignment, normalization,
  baseline correction, blank subtraction, or compound identification is done.

Malformed mzML that the reader cannot decode fails as a file-read error rather
than yielding invented trace points. A warning from the reader about declared
array-length disagreement remains visible in server logs.

## Resource bounds

The tool has a per-call `max_spectra` default and hard maximum of 5,000. This is
separate from `MSMCP_MAX_SPECTRA` and does not change limits for other tools.
It counts **all source spectra scanned**, including unselected MS levels and
scans outside the RT filter; it does not count selected scans or returned
points. At most 20 XIC targets are accepted. The existing allowed-root and
500 MiB file-size controls still apply. If the acquisition exceeds the
requested cap, the tool raises `SpectrumCountExceededError` and returns no
partial chromatogram. The reader streams one spectrum at a time; the returned
trace arrays are bounded by the scan limit.

## Zed / MassPilot integration

1. Ensure the Zed MSMCP server process has `MSMCP_ALLOWED_ROOT` set to a
   directory containing both acquisitions (or a parent directory). Keep the
   existing file-size policy in force.
2. Call `extract_chromatograms` separately for the sample and blank with the
   four targets above, `tolerance=5.0`, `tolerance_unit="ppm"`, `ms_level=1`,
   and `max_spectra=5000`.
3. Read `structuredContent.chromatograms.retention_times_min` as the shared
   coordinate vector and use the corresponding `tic_intensities`,
   `bpc_intensities`, and each `xic[i].intensities`. Use the XIC's
   `target_mz` to associate each vector with its requested ion.
4. Check `completeness.full_acquisition_processed`, `missing_observations`,
   `warnings`, source digests and acquisition counts before comparing traces.
   Keep the sample and blank as separate traces; MSMCP does not subtract one
   from the other.
