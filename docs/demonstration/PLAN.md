# Pre-publication demonstration plan — 2026-10-20

Scope: documentation and a replayable experimental demonstration, based on
origin/main 7d3e8a4f335ee25d4e4f3c600bf075ba02382c6a. Preserve the dirty canonical
checkout and overlapping PRs; do not port their algorithms or safety changes.

- [x] Audit README, architecture, validation evidence, providers, search and provenance.
- [x] Verify the public Zenodo mzML identity and CC BY 4.0 licence.
- [x] Replace invented README output with executable supported-format instructions.
- [x] Replay acquisition summary → MS2 reference → adduct/formula → precursor check
  through a new stdio server; independently decode XML/binary and calculate ppm.
- [x] Verify a supported MSP search on an attributed measured MassBank record.
- [x] Save actual tool transcripts, hashes, configuration and scientific limitations.
- [x] Capture fallback: explicitly record
  the capture blocker and provide a local Zed recipe, caption and conditional alt text.
- [x] Run lint, types, unit tests, notebook evaluation and documentation checks.
- [x] Review the scoped diff; commit and push only this branch; create one draft PR.

Handoff: `codex/prepublication-scientific-demo`;
[draft PR #66](https://github.com/janusson/MSMCP/pull/66), open and unmerged.
Screenshot remains incomplete under the documented capture restriction.

No algorithm changes, merges, releases or social publication. The script is a
transport replay, not an invented AI conversation or scientific-validation claim.
