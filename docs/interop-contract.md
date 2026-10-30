# MassFlow–MSMCP interoperability contract — issue #49

Verified on 2026-10-21 against MSMCP source `25439fbf927db12c790c322d76f2ea86414e99bb`
and analytical MassFlow source `ffef50bff913fcf400cb3de72595dab50e4f3fd8`.
This is a read-only design and a synthetic software probe. It introduces no
adapter, cross-package dependency or public API change.

## Compatibility

The identically named distributions are different projects. MSMCP main installs
the lowercase imaging package `massflow==0.1.2`; analytical MassFlow declares
distribution `massflow==0.1.0` and imports as `MassFlow`. An adapter must never
replace one with the other in the same environment.

| Boundary | MSMCP main | Analytical MassFlow main | Exchange requirement |
|---|---|---|---|
| Reader identity | `resolve_source` selects MSMCP mzML/MGF readers or the lowercase imaging backend | `MassFlow.io.load_spectra` uses matchms and annotation input validation | Keep separate interpreters; record distribution/version, module path and source fingerprints. |
| Representation | Frozen Spectrum dataclass with read-only float64 arrays and optional fields | Mutable matchms Spectrum with metadata and float64 peak arrays | Copy explicitly at a future boundary; preserve source arrays and identities. |
| MGF retention time | `RTINSECONDS=120` becomes 2.0 **minutes** | The same record retains 120.0 **seconds** | Convert exactly once; name the units and retain the original value. This observation does not establish every format's units. |
| Missing precursor | Preserved as `None` | Reader rejects the record with a reason | Separate raw ingestion from annotation eligibility; never invent zero or erase a raw scan silently. |
| Zero intensity | Preserved in raw ingestion | A spectrum containing a non-positive intensity is rejected | Carry original arrays plus downstream rejection, rather than substituting a filtered acquisition. |
| Other optional metadata | Index, MS level, RT, precursor, polarity and imaging coordinates may be absent | Metadata dictionary; subsequent processing may repair fields and apply chemistry validation | Preserve absence and record every conversion/repair. Imaging coordinates are not retention times. |
| Precursor validation | Parsing a selected-ion value is not a chemistry check | Input validation and the later fixed 5 ppm formula/adduct gate are separate stages | Record the stage, configuration and actual rejection reason. Neither parsing nor a high score establishes identity. |
| Library identity | Main's real-library registry supports MSP and MGF; unsupported paths can use a disclosed synthetic fallback | Explicit LibrarySpec with raw files or SQLite/Zarr/hybrid stores | Require an actual supported library on a real-data path. Preserve path identity, reader, count and digest or explicit absence. PR #62 overlaps the already-present MGF provider; SQLite remains in PR #64. The library module header still says only MSP, so use the executable registry rather than that stale sentence. |
| Provenance | Immutable operation record with parameters, software/model information, source and parent reference IDs; source digests may be absent above 64 MiB | Run schema v2 with config/lockfile/input/library hashes, engine, processing, seed and Git state | Retain both schemas and versions; an absent digest is not proof of identical input. |
| Scoring/confidence | Main still has production significance machinery; PR #72 proposes withdrawing its unsupported claims | Different scorers and independent confidence/configuration fields | Preserve scorer, tolerances and confidence status separately. Never equate scores or transfer p/q-values. Neither project's FDR is validated by this probe. |
| Local references | Opaque process-local IDs and immutable payloads | Spectrum/store objects and output paths | A reference token is not a portable file or cross-process identifier. |
| Filesystem/offline boundary | Allowed-root resolution, symlink checks and resource limits | Local Python reader has no MSMCP SecurityPolicy | A future bridge must reapply policy to inputs and sibling files. No runtime download or implicit dependency installation. |
| Stdio | Protocol owns stdout; imaging import handlers are redirected to stderr | This import/load probe emitted no extra stdout lines | Keep stdout clean. This measurement covers this probe, not every logging path. |

Source: the pinned repositories' `pyproject.toml`, MSMCP
`src/msmcp/{ingest,mgf,mzml,massflow_io,library,provenance,security}.py` and
`src/msmcp/state/pointers.py`, and analytical MassFlow
`src/MassFlow/{io,models,library,workflow}.py`. The executable measurement and
all Python source hashes are in
[`validation/interop-2026-10-21.json`](../validation/interop-2026-10-21.json).
The statements about open PRs are a dated integration warning, not a live board.

## Minimal executable contract

[`validation/contract_probe.py`](../validation/contract_probe.py) writes only a
temporary synthetic three-record MGF and its requested JSON output. Each project
runs in its own already-synced environment. The child cwd is temporary, containing
the imaging dependency's incidental log directory. No experimental data is read.
The report records interpreter/distribution versions, loaded module locations and
source SHA-256 hashes, so an installed distribution name cannot silently select
the other project.

Nine checks passed on the pinned sources: three MSMCP records; one accepted
MassFlow record plus two reported rejections; unchanged shared peaks/precursor;
the explicit minute/second difference; missing precursor preservation; preserved
zero intensity; float64 arrays; MSMCP immutability; and clean probe stdout.
These characterize software behavior on constructed peaks, not identification
accuracy, measured fragmentation, representative throughput or error control.

```bash
# Sync each project's own locked environment before the optional probe.
uv sync --extra dev --locked

# Run from MSMCP; select the analytical checkout explicitly.
make interop-check MASSFLOW_ROOT=/absolute/path/to/analytical/MassFlow

# Normal CI also exercises the evaluator and subprocess failure handling.
uv run pytest tests/test_interop_probe.py
```

The analytical checkout needs its own `uv sync --locked`. The probe does not
install packages. Hermetic tests fail on empty reader output, changed RT units
or precision, writable arrays, stdout contamination and duplicate structured
results; they also check source fingerprints and that input bytes are unchanged.
Existing security tests separately exercise outside-root/symlink/traversal and
compressed-input bounds; this probe does not replace those checks.

## Realistic end-to-end demonstration design

This is a proposed demonstration, not an executed experiment. Use a licensed,
user-approved local mzML acquisition and supported real MSP library. Resolve
both under MSMCP's allowed root; retain file identities, digests or missing-digest
reasons, the selected MS2 scan and its original reference. Score the real input
without synthetic fallback. Keep candidate rankings distinct from significance.

For a separately authorized analytical run, export through an explicit,
versioned exchange format. Preserve original intensities, precursor m/z and RT
units; record conversion once and every annotation rejection. Retain both
provenance records and compare arrays, precursor and normalized time before
interpreting rankings. Different matching algorithms need not agree numerically.
Keep all experimental files local. Independent compound/batch evidence is
required before making identification or FDR claims.

## Decisions before an adapter

1. Resolve distribution/import naming and installation coexistence; do not
   implement #10 by assuming the imaging package is the analytical engine.
2. Define a versioned exchange schema with explicit units, missingness, source
   identity, conversion history and reader/scorer identity.
3. Keep raw acquisition records and annotation rejections as separate evidence.
4. Review the existing library/parser and significance PRs together before
   claiming an integrated real-library workflow. Preserve uncalibrated status
   unless separate scientific validation supports a stronger claim.
