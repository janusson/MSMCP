---
name: ms-data-ingestion
description: Rules for reading mass-spectrometry files in msmcp: supported formats, the single dispatch point, reader semantics, and what must never be invented.
category: data
version: 1.0.0
---

# MS data ingestion

Use this when reading an acquisition or adding support for a new format. The
guiding constraint: **do not silently change mzML/MGF semantics, and do not
invent metadata.**

## One entry point

All ingestion goes through `ingest.resolve_source(path, policy)`. It picks the
reader from the file extension and returns a `Source` whose spectra are all the
same `msmcp.mzml.Spectrum` type, so no downstream tool knows or cares which
library parsed the file.

```
file -> ingest.resolve_source -> (MassFlow | msmcp reader) -> Spectrum -> DataReference
```

Do not call `massflow_io`, `mzml`, or `mgf` directly from a tool. Tools depend
on the interface (`resolve_source`), not the mechanism.

## Supported formats

| Format | Extensions | Reader / backend |
|---|---|---|
| mzML | `.mzML`, `.mzML.gz` | `msmcp.mzml` |
| MGF | `.mgf`, `.mgf.gz` | `msmcp.mgf` |
| imzML (imaging) | `.imzML` (+ `.ibd`) | MassFlow (`massflow.imzML`) |

The reader chosen is carried into provenance as `Source.backend`, which is what
makes it possible to tell later whether two results came from the same parsing
path.

## Unsupported formats

Vendor formats are recognised and rejected with actionable guidance, not a
generic failure:

| Suffix | Vendor |
|---|---|
| `.raw` | Thermo |
| `.d` | Agilent / Bruker |
| `.wiff` | SCIEX |
| `.mzxml` | legacy mzXML |

The error directs the user to convert with ProteoWizard MSConvert (except
imzML, which is passed through directly). A new `UnsupportedFormatError` case
should name the vendor and the next step, mirroring `ingest.py`.

## Reader semantics to preserve

- An **empty scan** is legal and must not abort a run. Real Bruker mzML files
  contain thousands of legitimately empty spectra; `tests/conftest.py` models
  this with the `"empty"` and `"absent"` payload modes.
- A file that **declares peaks but carries none** is corruption, not emptiness.
  Distinguish the two; do not treat a malformed payload as a valid empty scan.
- Empty arrays are spelled several legal ways in the wild (`<binary/>` with
  `encodedLength="0"`, or `<binary>` omitted entirely). Handle both.

## Metadata honesty

- Do not invent cvParams, retention times, MS levels, or precursor m/z that the
  file does not state. Report `None` when absent.
- Do not convert between units unless the file states the unit.
- Every read goes through the security boundary (see the `security-boundary`
  skill): allowed root, file size, and spectrum count all apply to ingestion.

## Tests

Ingestion tests build fixtures rather than committing binary data. See
`tests/conftest.py` for the mzML/MGF builders and `tests/test_ingest.py`,
`tests/test_mzml.py`, `tests/test_mgf.py`. The imzML fixture is written with
`pyimzml` (a MassFlow dependency) and skipped when that dependency is absent,
rather than faked.
