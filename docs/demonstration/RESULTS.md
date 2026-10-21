# Verification results — 2026-10-20

**State: verified computation and reproducible stdio replay; capture pending on Eric.**
**READY FOR ERIC TO CAPTURE.** The reproducible replay is the completed
computational deliverable. A human at a real MCP client performs the capture;
Eric follows [the numbered runbook](CAPTURE.md). The computer-use policy refusal
was the correct boundary and is not a computational failure or readiness blocker.
Handoff: [draft PR #66](https://github.com/janusson/MSMCP/pull/66), branch
`codex/prepublication-scientific-demo`; open and unmerged.

The [complete revised local gate log](2026-10-20-make-check.txt) preserves the
commands and outcomes; only the local checkout root is replaced by `<REPO_DIR>`.

## GitHub CI evidence

The **gate** on head `c0416dcd7965b1ccddefc86399a42a65b9f26f04`
[completed successfully](https://github.com/janusson/MSMCP/actions/runs/39514511771/job/118943803756)
from **2026-10-20 23:23:14 to 23:25:31 UTC (2m17s)**. The recorded steps passed
Ruff format/check, mypy, basedpyright, the fast pytest suite and `make eval`.
The [check-run metadata snapshot](2026-10-20-prior-head-ci.json) binds that
success to its exact head. The earlier “no CI checks” observation was transient
and is superseded by this completed check run; it is not the current CI status.
This is prior-head CI evidence, distinct from the revised local gate below.

Environment: macOS arm64, CPython 3.13.12; locked MSMCP 1.0.0 development
installation with mcp 2.1.1, numpy 2.4.6 and massflow 0.1.2. Server source is
unchanged from upstream commit `7d3e8a4f335ee25d4e4f3c600bf075ba02382c6a`.
The transcript records Git head `c0416dc` at execution; its server source is
unchanged from the upstream baseline, while the example and documentation were
being revised in the working tree. Its script and
lock hashes bind the actual replay code and dependency lock. No scientific
source changes or modifications to existing tests were made.

| Check / command | Observed outcome |
|---|---|
| `uv sync --locked --extra dev --offline` | Passed; independent worktree environment created and import path verified |
| Prior baseline `uv run pytest -q` | 613 passed, 1 deselected |
| Revised `make check` | Passed: `make lint` → `make test` → `make eval`; aggregate target added to mirror CI without formatting writes |
| Revised `make lint` | Ruff format/check passed; mypy passed 59 source files; basedpyright 0 errors/warnings |
| `uv run mypy examples/public_demo.py` | Passed (example checked explicitly) |
| `uv run basedpyright examples/public_demo.py` | 0 errors/warnings |
| Revised `make test` | 613 passed, 1 deselected |
| Revised `make eval` | Notebook PASSED: 118 checks passed, 1 skipped; pytest 1 passed, 1 deselected |
| `public_demo.py prepare` | Downloaded inputs and verified hashes; converted measured record without modifying peaks |
| `public_demo.py replay` | 12 actual calls over a newly spawned stdio MCP session; all returned without tool errors |
| Independent acquisition check | 4,747 spectra; 1,026 MS1 / 3,721 MS2; scan 591 has 18 peaks; TIC agrees within 1e-14 relative |
| Independent precursor check | Expected 205.0971541013389; observed 724.722412109375; signed +2533556.6467747683 ppm; REJECTED at the profile's 5.0 ppm window |
| Supported MSP smoke check | Measured favipiravir self-match, score 1.0000; real-disk banner and library digest verified |
| Corrupt-input negative check | Hash helper and `prepare` both refused an existing acquisition containing deliberately incorrect bytes, before conversion/server start |
| Source-scope check | `git diff --quiet c0416dc -- src tests pyproject.toml uv.lock` passed; no scientific source or existing test changes |
| Link checks | All relative file links resolve; linked external source URLs checked; prior-head CI link verified through the GitHub API |
| Artifact hygiene | No local user paths, embedded raw-file locations, instrument serials, credential patterns or binary datasets in the added evidence |
| `git diff --check` | Passed |

The notebook's skipped check requires the optional DreaMS package; no real
foundation-model inference is demonstrated. The independent MassBank similarity
validation was not rerun: scientific algorithms and its protocol did not change.
Existing partial validation and the known greedy discrepancy remain as documented
in [VALIDATION_SIMILARITY.md](../../VALIDATION_SIMILARITY.md).

The acquisition snapshot and library record identities, rights, attribution and
conversion are documented in [README.md](README.md). The unmodified Zenodo licence
metadata is preserved in [source metadata](2026-10-20-source-metadata.json).
The scan and hypothesis were fixed using the [prospective selection record](SCAN-SELECTION.md);
the same rule in the independent decoder selected index 590 without a mass-error filter.
Actual values and software provenance are in the [tool transcript](2026-10-20-transcript.json)
and [server diagnostics](2026-10-20-server.stderr.txt). The diagnostics use local
Pacific time; transcript provenance timestamps are UTC. The transcript retains
the search's uncalibrated p-value output verbatim; it is excluded from the claim.

This run establishes a real-data tool interaction and a reproducible numerical
check. It does not establish a structural identity, calibrated confidence/FDR,
instrument fitness, broad parser compatibility, or representative throughput.
The feature branch and draft PR hold documentation/example changes only; no
merge, release, registry publication, social post or deployment is included.
