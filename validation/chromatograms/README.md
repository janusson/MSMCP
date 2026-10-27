# MSV000089046 chromatogram software verification

This is a reproducible check of the structured extraction contract, not an
analytical method validation, compound identification, quantitative calibration,
or performance claim about other instruments or datasets. No raw data or full
trace exports are committed. The summaries contain public-data calculations.

## Inputs and authority

`MANIFEST.json` is copied verbatim from MassPilot PR #28 at
`07b46f78677e59aacc978da368ba948873fe82bf`, path
`examples/MSV000089046/MANIFEST.json`. Its primary source is the
[MassIVE dataset record](https://massive.ucsd.edu/ProteoSAFe/dataset.jsp?task=98f9d86b80414ac991063bb460f78ab2).
The two files under test are in
`raw/Orbi_CS_Metabolomics_Example_Set/Orbi_POS_DDA_MZML/`.
The verifier requires these exact byte sizes and full SHA-256 values before
starting a server; it never substitutes inputs or fetches data automatically.
Local cached files were rehashed on 2026-10-22. The dataset page timed out during
this run, so current remote availability was not confirmed; local byte identity
is independently checked against the pinned manifest.

| Role | File | Bytes | SHA-256 |
|---|---|---:|---|
| Sample | `Orbi_P_E2_CSSM10uMA.mzML` | 70,767,082 | `91a112752847048be77dc07ad2550af7ada9e02bbfcf71d365593f3d7fe6989b` |
| Blank | `Orbi_P_E2_EXBLANKA.mzML` | 65,242,379 | `93eddd49a943beed5c24dd3b96e1fa10fd2a1c85e17dec12532abffc5e8aa71f` |

## Original failure and repair

PR #63 baseline: `0f14835e178cf3c4601c9015aa3990f54f3ad9fa`.
`SourceRef.from_path()` defaulted `digest_limit_bytes` to 64 × 1024² =
67,108,864 bytes. `provenance.file_digest()` checked `stat().st_size` and returned
null above this threshold **before opening the file**. The sample is 3,658,218
bytes above that threshold. The tool returned traces but a null source digest
and a warning; MassPilot correctly refused to authenticate that evidence.
The blank remained below the threshold and produced its expected digest.
This was separate from the 500 MiB input policy and 10,000-spectrum ceiling.

The extractor now explicitly uses the existing configured file-size budget for
its mandatory streaming digest, rejecting authentication failures. No policy
is disabled or globally enlarged. Hashing was already streaming; merely
changing chunk size could not fix the original early size check. Raising the
shared default would affect unrelated tools, while prefix/size fingerprints or
silently accepting null would not authenticate the source. A separate new
budget was unnecessary because this tool already promises a bounded full-file
read under the existing operator-configured budget.

Source identity/change checks, declared-versus-observed counts, exact native scan
IDs and structured reader warnings close related contract gaps. TIC/XIC are
still sums of decoded intensities and BPC is still their maximum. Nulls, empty
scan zeros, minutes, source order and centroid/profile semantics are unchanged.
Schema 1.0 adds `native_scan_ids`; it now advertises the supported revision as a
constant. This is an unreleased draft interface; source pinning remains required.

## Reproduction

Use a separate checkout at the implementation commit recorded in `RESULTS.json`.
Provide the two exact files above in a local directory. To fetch them, use the
pinned MassPilot example's documented `fetch.py` workflow, or obtain them from
MassIVE and let this verifier check their identity. No MassPilot change is needed
to run this verification.

```bash
# Install the exact locked dependencies in this checkout.
uv sync --locked --extra dev
```

```bash
# Independently compare every selected point via a real MCP stdio child process.
uv run --no-sync python validation/chromatograms/verify_public.py \
  --data-dir /absolute/path/to/MSV000089046 \
  --output /tmp/msmcp-chromatogram-verification.json
```

```bash
# Run the repository's required static, unit and notebook gates.
make lint && make test && make eval
```

To reproduce the original absence separately, use the baseline checkout and run:

```bash
# Show the baseline's default source-digest decision without modifying inputs.
uv run --no-sync python - /absolute/path/to/Orbi_P_E2_CSSM10uMA.mzML <<'PY'
import sys
from msmcp.provenance import SourceRef
print(SourceRef.from_path(sys.argv[1]).to_dict())
PY
```

## Independent calculation and recorded evidence

`verify_public.py` imports no MSMCP reader or calculator. It decodes mzML XML,
base64/zlib and IEEE floats with Python's standard library and uses `math.fsum`
for TIC/XIC versus the tool's NumPy float64 summation. It reads RT unit accessions
explicitly and verifies centroid representation, native IDs, numeric indices,
source ordinals and declared scan counts. It refuses unexpected encodings/units;
it is a reference for these pinned files, not a replacement ingestion library.

Parameters were fixed before inspecting results: MS1, 5 ppm inclusive windows,
targets 195.0877, 223.0634, 297.0822, 299.0614; all 10,000 allowed source spectra.
Every selected MS1 point is compared, rather than only matching maxima.
Acceptance is relative error ≤1e-12 or absolute error ≤1e-8. Representative XIC
maxima are descriptive points selected after the all-point comparison, not
identity evidence. Repeated calls must agree except provenance timestamps.
`RESULTS.json` records exact versions, source commit, output-schema hash, counts,
errors, observed elapsed times, warnings and representative numeric values.

## Results recorded 2026-10-22

Implementation commit: `64cb1db69a2b6ba1369ee4eb7c6bd296979a3c56`. The verifier ran with a clean
source tree, checked this checkout's package/environment, forced the child to
load this checkout's `src`, and recorded SHA-256 fingerprints for all tracked
source files, the verifier and lock/project files. Full locked dependency
versions are in `uv.lock`; no dependency was changed.

Python 3.13.12; MSMCP 1.0.0 (source pin required), MCP SDK
2.1.1, NumPy 2.4.6, Pydantic 2.13.4,
and imaging `massflow` 0.1.2 (distinct from analytical MassFlow).

| Input | Source scans | Selected MS1 points | Maximum absolute numerical error |
|---|---:|---:|---:|
| CSSM10uMA sample | 8,357 | 2,090 | 0 |
| EXBLANKA blank | 7,916 | 2,100 | 0 |

All RT/TIC/BPC values and all four XIC vectors agreed exactly in this run.
Repeated stdio calls agreed except provenance timestamps. Both inputs reported
centroid MS1 data and no missing selected observations. At target 195.0877, the
sample maximum was 521,639,936 at source ordinal 1,540 (RT 2.3570816 min), versus
blank maximum 1,972,385.75 at ordinal 1,424 (RT 2.3506991 min). At target 299.0614,
all 2,100 blank values were valid zeros. These values are input-defined intensity
units and are not blank-normalized or identity assignments. See `RESULTS.json`
for each representative point's native ID and independently calculated TIC.

Verification commands and observed outcomes:

- Focused baseline: 72 passed before changes.
- Regression red phase: 10 failures exposed digest, identity, count/warning,
  argument and schema gaps before the fixes.
- `make lint`: Ruff formatting/checks, mypy (63 files), basedpyright all passed.
- `make test`: 662 passed, 1 evaluation test deselected.
- `make eval`: notebook 119 passed / 0 failed / 1 optional DreaMS skip;
  pytest 1 passed, 1 deselected.
- Public verifier: full sample and blank comparison passed over SDK stdio;
  output schema hash and exact numerical comparison are recorded.
- `git diff --check`: passed.

A read-only review identified two handoff weaknesses: stale schema prose and
ambiguous code provenance in the validation report. Both were corrected; the
final public verification was rerun from the committed implementation.

## Security, resource and scientific limits

The 500 MiB default is still the source/decompression budget, configurable only
by the operator's existing `MSMCP_MAX_FILE_SIZE_BYTES`. A lower limit rejects the
same sample before extraction. Digests read at most budget + 1 byte with ≤1 MiB
chunks; no complete-file buffer or extra dependency is added. Source growth or
replacement detected through size/device/inode/nanosecond time checks fails
without traces. Inputs must remain immutable; these checks are not an atomic
snapshot or a defence against privileged metadata restoration. SHA-256 binds
local bytes, not remote publisher authenticity or instrument completeness.

Existing allowed-root checks (including symlink resolution), regular/readable
file checks, gzip decoded-byte and zlib per-array bounds, all-source scan cap,
and target cap stay in force. Gzip digests identify compressed bytes. There is
no new server wall-clock deadline: storage latency can still block a synchronous
read, and a client timeout does not guarantee cancellation. Per-array memory can
reach the existing byte budget; this change is not a general memory-limit
redesign. No traces are returned on malformed data or resource failure.

Regression coverage includes >64 MiB valid XML, digest growth, absent digest,
source mutation, malformed/truncated XML, corrupt base64, missing arrays/list,
declared count mismatch, structured length warnings, invalid arguments/schema,
5 ppm endpoints and their immediately adjacent IEEE floats, missing RT/arrays
versus valid zeros, unknown/profile data, scan caps and real stdio. The stdio
regression additionally checks file-size, gzip expansion, root escape and
post-error server recovery. The wider repository retains its symlink and zlib
bomb regression tests.

These files have no missing observations in the selected MS1 points. Missing
observations are therefore tested with explicit synthetic fixtures, not claimed
to have been measured in the public acquisitions. Intensity units remain
`input_defined`; an XIC of zero means a valid window sum was zero, not that a
compound is absent. No blank subtraction, smoothing, identity, concentration,
calibrated FDR or analytical validation is inferred.

## MassPilot integration handoff

PR #63 remains draft, unmerged and unreleased. Released package version `1.0.0`
does not uniquely identify this code; use the exact implementation SHA in
`RESULTS.json` and the locked dependencies, then record the source SHA separately
from the package's version field. No MassPilot files were edited in this task.

After human review, the MassPilot owner can update its dependency pin, verify
that `tools/list` advertises this output schema, and replay PR #28's exact
CSSM10uMA/EXBLANKA workflow. Batch TIC/BPC and all targets in one call per file;
set `MSMCP_ALLOWED_ROOT` to the input directory and `max_spectra=10000`. Use
`structuredContent` only, check schema 1.0, hashes against the manifest, equal
array lengths, counts and `full_acquisition_processed`; preserve warnings/nulls
and all three scan identities. Translate `da` to `Da` if needed. Cache by full
digest and exact parameters plus implementation/schema version. A tool error,
unsupported revision, empty selection or missing observations must retain its
actual evidence status. Missing RTs must not be plotted at zero.

The schema addition is compatible with consumers ignoring unknown fields;
clients requiring native IDs must explicitly read the new parallel array.
Issues #20/#21 still need their own MassPilot-side acceptance review and a replay
against this dependency. This repository verification does not close them, and
no downstream replay or merge/release/deployment was performed here.
