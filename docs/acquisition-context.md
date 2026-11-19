# Acquisition context in spectrum and library-search results

`load_spectrum` returns the acquisition record in
`provenance.source_acquisition`. A spectrum reference retains the same record.
The spectrum summary, reference summary, QC summary and completed library-search
report display precursor activation. Read the structured record to inspect
individual scans, selected ions, components and source units. QC groups the
activation labels for its first 20 processed spectra and states the coverage.
Text summaries show up to four precursors in source order.

## Evidence and interpretation

The mzML reader records declarations at the place they apply. A scan can
override the run's default instrument configuration. Components describe the
referenced instrument configuration. A quadrupole and an Orbitrap in that list
do not establish which component measured a particular peak. An instrument
model name supplies no resolving power, source temperature or collision energy.

| mzML scope | Supported PSI-MS declarations | Interpretation |
| --- | --- | --- |
| Spectrum | MS level 1000511, positive/negative scan 1000130/1000129 | Preserve each declaration, including contradictions. |
| Scan | Start time 1000016, mass resolution 1000011, mass resolving power 1000800 | Preserve reported values and units. Resolution and resolving power have distinct meanings. Neither establishes measured performance. |
| Instrument source | Electrospray 1000073, nanoelectrospray 1000398, inlet temperature 1002040 | Source type and reported condition. Polarity alone supplies neither. |
| Instrument analyser | Quadrupole 1000081, TOF 1000084, Orbitrap 1000484, linear ion trap 1000291 | Preserve component order and the configuration reference used for that scan. |
| Selected ion | Selected-ion m/z 1000744, charge state 1000041 | Preserve each ion within its precursor and the source charge value. |
| Isolation window | Target 1000827, lower offset 1000828, upper offset 1000829 | Offsets are relative to the target. Derived endpoints require all three unambiguous finite quantities in m/z and a physically admissible interval. |
| Precursor activation | CID 1000133, beam-type CID 1000422, higher energy beam-type CID 1002481, ETD 1000598 | Keep methods grouped by precursor. Multiple methods may describe one dissociation sequence. |
| Precursor activation energy | Collision energy 1000045, normalized collision energy 1000138, generic activation energy 1000509 | Interpret collision energy in eV and normalized collision energy as a percent setting. Retain generic activation energy as its own declaration. |

The numeric identifiers in the table have the `MS:` prefix. Each retained term
carries its accession and any source name, lexical value, unit accession, unit
name and parameter-group reference. The model preserves local and external
spectrum references as identifiers. It follows local parameter groups and the
scan/run configuration reference. A dangling configuration reference yields no
components. Unresolved parameter-group IDs are listed in `unresolved_groups`.
A recorded value describes the declarations that could be read.

The wire record omits empty optional fields. An empty precursor or scan still
occupies its original position. Each precursor has separate
`collision_energy_ev` and `normalized_collision_energy_percent` records.
`reported` has a numeric value. `unknown`, `invalid_value`, `unknown_unit`
`multiple_values` and `unresolved_reference` carry no usable value. Percent and eV have no general
conversion. Unitless values, unknown CV accessions and free-text labels cannot
supply either quantity. A generic 0 eV activation-energy term can coexist with
unknown collision energy. `derived_isolation_bounds_mz` is computed from the
reported target and offsets. The existing scalar retention time is in minutes.

The MGF acquisition record retains the last block-local PEPMASS, CHARGE, Z,
MSLEVEL, IONMODE, RTINSECONDS and RTINMINUTES strings, including the intensity
token of PEPMASS when present. Missing keys serialize as null. The existing
numeric fields continue to follow the released reader. A charge sign supplies
no inferred polarity. A direct peak list or imaging spectrum has no typed
mzML/MGF acquisition record.

## Architecture and v1.1 evidence

| Criterion or boundary | Implementation and evidence | Qualification |
| --- | --- | --- |
| Report acquisition parameters bearing on comparability | Optional immutable record on `Spectrum`. `test_source_oracle` checks two external mzMLs. `test_load_reference_qc_and_search_reports` exercises registered I/O and QC tools, reference resolution and the search worker. | Partial. See the supported CV subset above. |
| Expose absence without implying query/library comparability | Each precursor reports unknown collision energy when the supported CV evidence is missing. The library report explicitly labels activation comparability unknown. Sparse upstream conversion, sparse MGF, invalid energy and multiple-scan tests exercise missingness and grouping. | Library entries have no unified activation record. MSP retains free-text metadata, the MGF provider projects selected fields and SQLite reads its store schema. |
| Record parameters in provenance for the result using them | The source record sits alongside operation parameters. Search retains its existing eligibility projection, declared precursor tolerance and actual comparison flag. The configured fragment window and the window used in classical scoring have distinct keys. | File and stored-reference queries carry acquisition evidence. New activation and instrument fields are descriptive. They supply no additional score, exclusion, probability or calibration. |
| Issue 16 dependency and compatibility criteria | Standard library XML and dataclasses. Existing scalar fields and constructors continue to work. Existing search eligibility and reader tests run in the repository gate. | Independently checked source declarations establish parser fidelity only. |

`QueryContext` still carries the released scalar projection for candidate
eligibility. It can represent fewer precursors and scans than the full source
record. In an ambiguous spectrum, consult the ordered source declarations
before interpreting a match. `InstrumentProfile` supplies software choices,
including the diagnostic-ion window used by QC. Those choices provide no
evidence of the instrument that acquired the file.

The reader preserves `scanSettingsRef` as a link. Planned inclusion lists,
external scan dereferencing, vendor filter strings, other activation methods,
energy ramps and inferred parameters remain outside this slice. No pinned
acquisition here independently verifies normalized collision energy, resolution,
inlet temperature, ETD or explicit scan-level configuration overrides. The
tests with edited XML exercise parser behavior. The upstream fixtures cannot
establish actual delivered energy, instrument calibration, identification
accuracy or consistency of an instrument's exported annotations.

The schema defines the scan override and run default. Term meanings and units
follow the pinned PSI-MS CV. [mzML 1.1.1 schema](https://github.com/HUPO-PSI/mzML/blob/47c499dad822c63e86c0c2d030ccfffa9f6c28f5/schema/schema_1.1/mzML1.1.1.xsd)
and [PSI-MS controlled vocabulary](https://github.com/HUPO-PSI/psi-ms-CV/blob/a40cf7541ee1dd61cc79c64a24da3bd73b91b746/psi-ms.obo).
