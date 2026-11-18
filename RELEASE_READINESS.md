# MSMCP 1.0.2 release readiness

Status: **published — `msmcp 1.0.2` is on PyPI as of 2026-11-09.**

`main` is at `4aa052001b4449f575ea13c0a7907394479a3dae` (the 1.0.2 candidate plus PR #89) and the annotated tag `1.0.2` names exactly that commit. The tag run ([`42564654644`](https://github.com/janusson/MSMCP/actions/runs/42564654644)) passed `gate and build` — the full gate, the tag-names-the-packaged-version check, and `uv build` — then the `upload to PyPI` job was approved and published. Both artifacts match byte for byte the ones that run built, and an independent rebuild from the tagged tree reproduces the same digests:

| File | sha256 | Uploaded |
|---|---|---|
| `msmcp-1.0.2-py3-none-any.whl` | `92500b39002a4676395f9264fa952bab55bd6e32755952eb1ad8ac679c6c9bdf` | 2026-11-09T17:57:19Z |
| `msmcp-1.0.2.tar.gz` | `99a6ec43c7361b4357699900d5eabe0bcb7f60a2b1e22080fd089f9048f3812e` | 2026-11-09T17:57:21Z |

PyPI lists `1.0.2` as the only release (`requires-python >=3.13`). A clean Python 3.13 install from the index advertises 15 tools, answers `ping`, and returns the expected chemistry (`[M+H]+` = +1.007276 Da; caffeine `C8H10N4O2` monoisotopic 194.0804 Da; a 0.0006 Da precursor error reported as 3.09 ppm).

### Installed-package qualification

`make validate-installed QUALIFY_SERVER=<venv>/bin/msmcp` runs
[`validation/installed_mcp_qualification.py`](validation/installed_mcp_qualification.py)
— the evidence script that drives the *installed* server with a real MCP stdio
client, rather than importing the source tree — against the artifact installed
from the index. The 1.0.2 run is committed as
[`validation/installed-1.0.2-2026-11-09.json`](validation/installed-1.0.2-2026-11-09.json)
with machine paths scrubbed. It records: 15 tools; an MGF query searched against
a real MSP library read from disk with the precursor gate enabled; provenance
naming `msmcp 1.0.2`, `mcp 2.3.0`, `numpy 2.5.4` and Python 3.13.12; adduct
chemistry for `[M+2H]2+`, `[M+2Na-H]+`, `[M+3H]3+` and `[M+Na-2H]-`;
`C6H12O6` at 180.0634 Da; and structured errors for an out-of-root path, a
malformed library, an MS1 query and an over-budget indexed read, with the
session still healthy afterwards. It characterizes packaging, protocol and
error-path behaviour: it is not a compound-identification or false-discovery
validation.

The receipt is evidence about the **published artifact** — installed from the
index into a clean virtual environment. The unit, evaluation and coverage
figures elsewhere in this repository come from the **development checkout** and
say nothing about the contents of the published wheel, or the reverse.

The four earlier `workflow_dispatch` attempts (2026-10-21 ×3, 2026-10-25) failed at the upload step with `invalid-publisher`; registering the pending trusted publisher for `janusson` / `MSMCP` / `publish.yml` / `pypi` resolved it. No owner action remains for 1.0.2.

### Tag history correction

Two tags were deleted on 2026-11-09 because their trees did not declare the version they named: `1.0.1`, and the earlier `1.0.2`. Both pointed at commits whose `pyproject.toml` reads `version = "1.0.0"`, so `publish.yml`'s tag/version guard would have rejected either. Restore one with `git push origin <commit>:refs/tags/<name>` if it is needed:

| Deleted tag | Tag object | Commit |
|---|---|---|
| `1.0.1` | `f07ce092dbe43aae6f8210600830380d0d1017a6` | `3d031cf7d854c6a24742af5a89d283ca8c3272ae` |
| `1.0.2` (pre-correction) | `8af8b905b563905e61f81a0077653df02afe5094` | `8e34387f12dfe7de1ace6fa13aa8105480317407` |

*Historical:* the qualification packet was refreshed on 2026-10-31 after PR #86. The corrected code tree was identical to the reviewed PR #86 source tree `af1d79a4cfeecb0b451ba522ffac2bd7ba87e82f`; that merge added no code changes, and hosted gate run `41253058734` passed on it. The detailed packet is [`docs/release-qualification/QUALIFICATION_REPORT.md`](docs/release-qualification/QUALIFICATION_REPORT.md). The identity and scope sections below still describe the released tree.

## Candidate identity

> **Dated clarification (2026-11-12).** The table below records the candidate as
> it stood *before* the upload, and its `PyPI` row — "not published — no trusted
> publisher registered" — describes the 2026-10 state, not the outcome. The
> header at the top of this file is the outcome: registering the pending trusted
> publisher resolved the earlier `invalid-publisher` failures, the tag run was
> approved, and `1.0.2` has been on PyPI since 2026-11-09. The row is left as
> written because this file is a record; only this note is new.

| Item | Value |
|---|---|
| Repository | `janusson/MSMCP` |
| Candidate branch | `release/1.0.2-candidate` |
| Base | `origin/main` @ `18e183a5873d6622b0cd09432b4056d13b0f3499` |
| Qualified code tree SHA | `e9a1599402143dd211fa64adb2b3f260ea574836` (merge tree equals PR #86 source tree `af1d79a4cfeecb0b451ba522ffac2bd7ba87e82f`) |
| Released commit | `main` @ `4aa052001b4449f575ea13c0a7907394479a3dae` (1.0.2 candidate + PR #89) |
| Version tag | `1.0.2` (annotated, pushed 2026-11-09) |
| Package version | `1.0.2` |
| PyPI | **not published** — no trusted publisher registered |
| Python requirement | `>=3.13` |
| Core dependencies | `mcp[cli]>=2.2.0`, `pydantic>=2.12.5`, `numpy>=1.26.4` |
| Optional extras | `imaging` (`massflow>=0.1.2,<0.2`), `chem`, `ml`, `test`, `dev` |

The tagged tree declares `1.0.2`, which is what the tag names. Earlier candidate branches (`release/1.0.2-candidate`, PR #82) are superseded by the merge onto `main`.

## Supported core server

The candidate advertises 15 tools on stdio:

`ping`, `load_mzml_summary`, `list_spectra`, `extract_chromatograms`,
`load_spectrum`, `summarise_reference`, `release_reference`,
`predict_adduct_offset`, `annotate_isotopes`, `validate_precursor`,
`compute_cosine`, `generate_qc_summary`, `search_library`,
`check_search_status`, `cancel_search`.

Core scope: local stdio MCP, deterministic chemistry and scoring, supported
mzML/MGF ingestion (plus bounded chromatogram extraction), MSP/MGF library search
with explicit acquisition-context gating, bounded data references and provenance,
bounded resource/security limits, and a documented real-client workflow.

Optional and explicitly not release-blocking: imzML via `msmcp[imaging]`,
foundation-model scorers with real weights, remote hosting, durable jobs, MCP
Tasks dispatch, and future protocol features.

## Integrated changes

| Work | Result in candidate | Notes |
|---|---|---|
| #81 mzML scan-time/array integrity | Integrated | Strict unit accessions, paired-array validation, declared-length reporting; tests updated to the strict reader |
| #70 imaging dependency/name collision | Integrated | `massflow` moved to `msmcp[imaging]`; core wheel has no `massflow` requirement |
| #71 indexed-read limits | Integrated | `load_spectrum`/file search reject indices beyond `MSMCP_MAX_SPECTRA` before scanning |
| #69 bounded QC | Integrated | Acquisition scan stops at the cap and discloses unconfirmed full coverage |
| #67 invalid scoring inputs | Integrated | Non-finite/negative peaks rejected at the scorer boundary and before search dispatch |
| #72 significance withdrawal | Integrated | No production p/q values, significance filter or FDR claim; contract `msmcp.search.similarity.v1` |
| #63 chromatogram extraction | Integrated | Bounded authenticated mzML extraction; verified against the corrected #81 reader, including scan-time units and array pairing |
| #75 MassFlow interoperability contract | Integrated | Reproducible opt-in probe, contract note and tests; no dependency exchange |
| #60 documentation/packaging | Integrated | Readiness/container docs and contract tests; reconciled with the optional imaging boundary |
| #68 citation metadata | Integrated | `CITATION.cff` |
| #84 SQLite schema documentation | Integrated | Documents the existing read-only SQLite provider and marks the verified #2 library coverage criteria complete |
| #65/#74/#76/#77/#78 dependency updates | Integrated | Lockfile now uses MCP 2.3.0, Pillow 13.0.0, filelock 4.2.4, opentelemetry-api 1.45.1 and setuptools 84.2.1 |
| Issue #79 context eligibility | Implemented in this candidate | MS1 refusal, precursor/polarity/charge/adduct gate, unknown-context labelling, eligibility counts and applied-tolerance disclosure |

Existing PRs are reused by integrating their commits; this candidate does not
rebuild their functionality. The original PRs remain open until the owner
authorises the integration PR.

## Issue #79 evidence

Before, on `origin/main`:

- `SearchRequest` had no MS-level, precursor, charge, adduct or isolation fields.
- A classical scorer returns `1.0` for a query peak at `m/z 50` against a
  library entry whose precursor is `900`, and `0.0` for the entry whose precursor
  is `100`. Fragment similarity alone cannot distinguish them.
- Search provenance advertised a precursor tolerance even though no precursor
  gate was applied.

After, on the candidate:

- `QueryContext(ms_level=2, precursor_mz=100, polarity="positive")` assesses the
  900 entry as `excluded` with `precursor outside declared tolerance`, and the
  100 entry as `matched`.
- An MS1 query is refused before dispatch.
- `tests/test_search_eligibility.py` pins the MS1/M3 refusal, 100-versus-900
  precursor exclusion, polarity exclusion, missing-context labelling, near-match
  acceptance, direct peak-list exploratory mode, charge/adduct conflicts and the
  provenance counts.
- The installed-client qualification reproduces the exclusion over real stdio:
  `Context eligibility: 0 matched, 1 unknown, 2 excluded before ranking
  (reasons: precursor outside declared tolerance (1), polarity mismatch (1)).`

Scientific limitation: this is a software eligibility contract. It does not
establish identification accuracy or a calibrated false-discovery rate.
`VALIDATION_SIGNIFICANCE.md` records the restoration conditions.

## Final chemistry correctness gate

The chemistry review defects are corrected in code commit
`793c05d2ee3c5fb610b73b78981c68681bf708ab`:

1. **Formula parsing:** `_parse_formula` now walks contiguous tokens and rejects
   any skipped/trailing text, malformed count and zero/negative count. Repeated
   elements remain valid and accumulate (`CH3CH3` → `C2H6`). Regressions include
   `C6H12O6!`, `C6H12O6?`, `C6-H12O6`, embedded whitespace, leading digits,
   fractional counts, and zero/negative counts.
2. **Adduct mass bookkeeping:** all 14 canonical adducts are now computed under
   one convention: neutral-atom composition delta minus signed net charge times
   electron mass; electron binding/ionisation energies are neglected. The
   independent test oracle uses separate NIST/CIAAW neutral atomic masses and a
   CODATA electron mass, not production constants. It checks every shift and
   the mass-to-charge divisor uses absolute charge. Corrected examples:
   `[M+2H]2+` = `+2.014553 Da`, `[M+3H]3+` = `+3.021829 Da`,
   `[M+2Na-H]+` = `+44.971165 Da`, `[M+Na-2H]-` = `+20.974668 Da`.
   Independent 500 Da neutral examples give 251.007276452 and 167.673943119
   m/z for the 2+ and 3+ protonated ions respectively.
3. **SMILES:** the mock table and formula-string passthrough are removed.
   Without RDKit or when RDKit rejects an input, conversion returns failure;
   explicit formula input remains core behavior.

Chemistry verification:

- Core environment (RDKit absent): `tests/test_chem.py` → 69 passed, 1 skipped;
  the skipped test is the actual RDKit-enabled conversion test. Simulated
  no-RDKit tests prove `CO` does not become formula `CO`, and formula input still
  works.
- RDKit environment: `uv sync --extra dev --extra chem` installed RDKit 2026.3.5;
  `pytest tests/test_chem.py` → **70 passed**. Actual RDKit resolves `CO` to
  `CH4O` (methanol), `CCO` to `C2H6O`, and rejects malformed SMILES.
- Installed core wheel MCP smoke: explicit glucose formula returns 180.0634 Da;
  malformed `C6H12O6!` returns an error; `CO` with `is_smiles=True` fails closed;
  the corrected multiply-charged and sodium-exchange adduct shifts match; all
  15 tools initialize and the full ingest/QC/search/provenance/errors workflow
  remains healthy.
- Installed wheel with `[chem]` MCP smoke: RDKit 2026.9.1 resolves `CO` to
  `CH4O`; explicit formulas and adduct values also pass. The sdist core install
  separately completed the same MCP chemistry checks, with RDKit and `massflow`
  absent.

## Deferred, not shipped

- #61 optional MS-Numpress decoding: remains optional; core mzML behavior is
  unchanged and Numpress input still reports a missing optional dependency.
- #62 MGF library support: current main already provides an MGF library provider
  through the SQLite/library work; the older PR is not needed for this candidate.
- #66 experimental workflow demo: documentation-only; not required for the core
  release.
- #65, #74, #76, #77, #78 dependency updates: incorporated into this candidate.
  The candidate lock now pins versions beyond the original PR targets where the
  resolver selected newer compatible patched releases; see the integrated changes
  table and gate results below.
- imzML/imaging, foundation-model inference, remote hosting and durable jobs:
  optional, unvalidated here, and not claimed as release features.

## Gate results

Run in the isolated candidate worktree with its own `.venv`:

| Gate | Command | Result |
|---|---|---|
| Format/lint/types | `make lint` | PASS — 96 files formatted; Ruff clean; mypy (71 source files) clean; basedpyright 0 errors / 0 warnings / 0 notes |
| Unit/integration | `make test` | PASS — 772 passed, 18 skipped, 1 deselected |
| Evaluation notebook | `make eval` | PASS — 119/120 cases passed, 1 expected optional DreaMS skip |
| Bounded decompression regression | `uv run pytest -q tests/test_security.py` | PASS — 32 passed, including capped `read`, `read1`, `readinto` and sized-read preservation |

Core-environment skips include the optional RDKit conversion test, imaging
backend tests and `scipy` for an optional Spearman reference. Actual RDKit
behavior is independently covered by the optional-chem run above; none is a core
dependency.

## Distribution qualification

Built from the candidate:

| Artifact | SHA-256 |
|---|---|
| `msmcp-1.0.2-py3-none-any.whl` | `92500b39002a4676395f9264fa952bab55bd6e32755952eb1ad8ac679c6c9bdf` |
| `msmcp-1.0.2.tar.gz` | `ca28702d6efae8eb8021233ab27c88923ebf9b346ead4a85d93fcfced8b3916e` |

Verified independently for wheel and sdist:

- Fresh Python 3.13.12 virtual environments outside the checkout. This is the
  only Python version qualified here; `requires-python = ">=3.13"` is the
  declared install range, not evidence from a multi-version compatibility run.
- `msmcp==1.0.2` installed; `massflow` **not** installed.
- Core install resolves MCP 2.3.0; the final wheel also passed with exact MCP 2.2.0, the declared minimum. Python 3.13.12 was used.
- `msmcp` entry point starts with `MSMCP-MassFlow-Adapter v1.0.2`.
- Wheel metadata lists `massflow` only under the `imaging` extra and `rdkit`
  only under `chem`. The candidate-only `RELEASE_READINESS.md` is excluded from
  the sdist and is not shipped as user documentation.
- `uv sync` / `uv run msmcp` from a source checkout remains the documented path
  until the package is published.

## Real MCP client evidence

The final wheel and sdist were each installed in fresh Python 3.13.12 environments and exercised through the MCP SDK stdio client (`validation/release_qualification_client.py`). The workflow discovered and called all 15 core tools; it tested synthetic mzML/MGF/MSP ingestion, structured chromatograms, reference create/read/release, chemistry, scoring, QC, real async dispatch/poll, cancellation, and bounded errors. A separate final-wheel run with MCP 2.2.0 passed. The query/library fixtures are synthetic software fixtures, not identification evidence. Full result and limitations are in `docs/release-qualification/QUALIFICATION_REPORT.md`.

The security correction now caps decompressed `read`, `read1`, and `readinto` requests to the remaining budget plus one sentinel byte before over-budget data can be materialized. The focused deterministic security suite passed 32 tests. Expected invalid tool calls still produce detailed server exceptions on stderr; the MCP wire error is generic and the local path disclosure is a documented diagnostic limitation.

## PyPI publishing

Failed run: [`40208250252`](https://github.com/janusson/MSMCP/actions/runs/40208250252),
dispatched on `main` `18e183a` for tag `1.0.0`. The `gate and build` job passed
every step (format, lint, both type checkers, tests, evaluation, version/tag
match, build). The `upload to PyPI` job failed with:

```
invalid-publisher: valid token, but no corresponding publisher
```

The OIDC claims were:

- `repository`: `janusson/MSMCP`
- `repository_owner`: `janusson`
- `workflow_ref` / `job_workflow_ref`: `janusson/MSMCP/.github/workflows/publish.yml@refs/heads/main`
- `environment`: `pypi`

The GitHub environment `pypi` exists. `https://pypi.org/pypi/msmcp/json` returns
404, so the project has never been created. This is a PyPI account configuration
failure, not a build, test or package-content failure.

**Exact account-level step required:** sign in to PyPI as the owner of `janusson`,
open **Publishing → Add a new pending publisher** (or the project's *Publishing*
settings if the project exists), and create:

| Field | Value |
|---|---|
| PyPI project name | `msmcp` |
| Owner | `janusson` |
| Repository name | `MSMCP` |
| Workflow name | `publish.yml` |
| Environment name | `pypi` |

The workflow name is the filename only, not `.github/workflows/publish.yml`.
The environment name is lowercase `pypi`. Do not create an API token or add a
long-lived secret: the workflow is intentionally token-free and OIDC-only.

After that configuration, the publish path for this candidate is: merge the
approved candidate, create the `1.0.2` tag on the merged commit, push the tag (or
dispatch `publish.yml` with `tag: 1.0.2`), and confirm the publish job. None of
those actions is performed by this task.

## Website

Repository `janusson/msmcp-site`, reconciled branch `release/site-1.0.2`:

- Base `origin/main` @ `045130abda2dabf4eb6e271d821c65394c544e12`.
- Tip `c7c3fa1bfb22d1b203e4b6330e07c2dcb19f9e33` (reconciliation + deployment checklist).
- PR #2's conservative claims and workflow are preserved, reconciled with the
  newer visual design and install-in-hero layout on main.
- The historical q-value table, timings and real-acquisition transcript are
  removed. The page now uses an explicitly illustrative workflow.
- The page lists all 15 tools, shipped chromatogram extraction, the context gate,
  the optional imaging boundary and source-install instructions.
- `make all` passes: Astro check, Prettier, production build, and 38 static
  contract checks (anchors, tool rows, claims, canonical metadata, robots,
  sitemap, assets, link hygiene, no client script, no welded words).
- `npm audit` reports 0 vulnerabilities after upgrading Astro to the patched
  7.4 line and fixing the dev-only `fast-uri` advisory.
- Desktop (1440 px) and mobile (390 px) renders were checked in headless Chromium:
  no document-level horizontal overflow, navigation reaches every section, and
  the full page renders with the existing visual design.
- Cross-links verified: `github.com/janusson/MSMCP`, `github.com/janusson`,
  `www.ericjanusson.ca`, `msmcp.dev` canonical/OG, `docs.astral.sh`, Google Fonts.
- Deployment will happen only after an authorised merge to site `main`. The site
  deployment checklist is `SITE_DEPLOYMENT_CHECKLIST.md` on the site branch.

## Known follow-ups (recorded, not part of this release)

1. **Optional-dependency paths are never exercised.** `rdkit`, `massflow`, `torch`
   and `dreams` are absent from every environment used here, so 18 tests skip and
   the `chem`, `imaging` and `ml` extras are unqualified. A matrix job per extra
   is the obvious fix; it is deliberately not added here.
2. **Resource leaks in tests.** `pytest -W always::ResourceWarning` reports 38
   warnings on the current suite, all pre-existing and none affecting results:
   19 unclosed SQLite connections from
   `tests/test_library.py::TestSQLiteLibraryProvider::test_peak_batches_respect_sqlite_variable_limit`,
   and unclosed gzip `FileIO` handles behind the compressed-input tests in
   `test_mgf.py`, `test_mzml.py`, `test_security.py`, `test_library.py` and
   `test_zed_config.py`. They surface late under a runner that treats warnings
   as errors.
3. **Coverage is diagnostic only.** The suite covers 91% of `src/msmcp`
   (`mzml.py` 82%, `models/backends.py` 84%, `tools/chem.py` 88%). No threshold is
   enforced, deliberately: boundary and scientific correctness matter more than a
   number, and a threshold invites tests written to clear it.

## Remaining blockers and risks

1. **PyPI publication.** The merge and the `1.0.2` tag are done; the upload is
   not. It needs the account-level trusted publisher below, and an approval of
   the `pypi` environment deployment.
2. **PyPI pending publisher.** The invalid-publisher error cannot be fixed from
   the repository; the account-level publisher above must be created by the owner.
3. **Diagnostic detail over MCP.** The SDK masks exception text in `is_error`
   results. Structured failure status is visible, but the exact reason is only on
   stderr. This should be addressed in a follow-up if clients need the text.
4. **Scientific scope.** Search is similarity-ranked, not a calibrated
   identification or FDR procedure. Context gating depends on metadata the source
   declares; missing context is labelled unknown rather than inferred.
5. **Optional capabilities.** imzML, real embedding weights, remote transport and
   durable jobs are not qualified by this candidate. The optional extras
   (`rdkit`, `torch`, `massflow`) are not installed by a default `pip install`
   and are not audited as a set; as of 2026-11-09 no advisory was open against
   them on the default branch either. The candidate does not claim a
   dependency-security audit beyond the core install.
6. **Default-branch security read-back.** Dated observation, not a guarantee: on
   2026-11-09 GitHub reported **0 open** Dependabot alerts on `main` (75 fixed, 0
   dismissed), which closes the 59-open figure (5 critical, 24 high, 27 medium,
   3 low) recorded earlier. On the same date `pip-audit` against the 37-package
   runtime closure installed from the published wheel reported no known
   vulnerabilities. Both are readings of that moment: a later advisory against
   one of those versions would not be reflected here, and this does not extend
   to the optional extras, which were not audited as a set.
7. **Website deploy trigger.** Merging the site branch to `main` triggers the
   existing Pages workflow by design; do not merge until the owner approves the
   deployment.

## Recommendation

**CONDITIONAL GO for the scoped, similarity-only 1.0.2 candidate after current PR #82 hosted checks and owner approval.** PR #86 is integrated at the qualified code tree; local gates, reproducible artifacts and fresh installed-client tests pass. Production search has no calibrated p/q/FDR claims. Optional extras, experimental significance inference, and MassPilot integration remain outside the validated release scope.

**NO-GO for tagging, PyPI publication, or website deployment until separately authorized.** The owner must verify PyPI Trusted Publisher registration (`msmcp` / `janusson` / `MSMCP` / `publish.yml` / environment `pypi`) and GitHub environment protection; neither can be confirmed from repository source.

## Draft review artefacts

- MSMCP release PR: https://github.com/janusson/MSMCP/pull/82
  - PR #86 was merged into `release/1.0.2-candidate` at `e9a1599402143dd211fa64adb2b3f260ea574836`; its tree matched the qualified source tree.
  - Hosted run [41253058734](https://github.com/janusson/MSMCP/actions/runs/41253058734): `gate` passed on the post-merge candidate code tree. PR #82's live head and any later documentation-only check must be verified directly on GitHub before owner approval.
  - Current local gates: `make lint` passed; `make test` 772 passed / 18 skipped / 1 deselected; `make eval` 119/120 passed with the optional DreaMS skip.
  - Current artifact hashes and installed MCP evidence are in the qualification report above; the lower historical receipts in earlier versions of this document are superseded.
- Site draft PR: https://github.com/janusson/msmcp-site/pull/3
  - CI run 40430041353: `Gate` passed (16s), `Build` passed (21s), `Deploy`
    skipped as designed for a pull request
- Site deployment checklist: `SITE_DEPLOYMENT_CHECKLIST.md` on the site branch

## Stop point

PR #82 remains open for final owner review. PR #86 is integrated; no merge to `main`, release tag, PyPI upload, publishing workflow dispatch, or website deployment has been performed.
