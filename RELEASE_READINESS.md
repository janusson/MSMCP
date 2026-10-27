# MSMCP 1.0.2 release readiness

Status: **candidate ready for approval review**. No merge, tag, PyPI upload or
website deployment has been performed. The recommendation is at the end.

Prepared 2026-10-26. This document describes the integrated candidate, not a
released version. `main` is unchanged.

## Candidate identity

| Item | Value |
|---|---|
| Repository | `janusson/MSMCP` |
| Candidate branch | `release/1.0.2-candidate` |
| Base | `origin/main` @ `18e183a5873d6622b0cd09432b4056d13b0f3499` |
| Code candidate SHA (chemistry-corrected) | `793c05d2ee3c5fb610b73b78981c68681bf708ab` |
| Package version | `1.0.2` (candidate only; no tag) |
| Python requirement | `>=3.13` |
| Core dependencies | `mcp[cli]>=2.0.0`, `pydantic>=2.12.5`, `numpy>=1.26.4` |
| Optional extras | `imaging` (`massflow>=0.1.2,<0.2`), `chem`, `ml`, `test`, `dev` |

The readiness document is committed after the code tip; the draft PR head is
therefore a later documentation commit. The code candidate SHA above identifies
the complete release source.

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
- #65, #74, #76, #77, #78 dependency bumps: not release-blocking; the candidate
  was qualified against a fresh resolution (`mcp 2.3.0`, `numpy 2.5.4`,
  `pydantic 2.14.1`).
- imzML/imaging, foundation-model inference, remote hosting and durable jobs:
  optional, unvalidated here, and not claimed as release features.

## Gate results

Run in the isolated candidate worktree with its own `.venv`:

| Gate | Command | Result |
|---|---|---|
| Format/lint/types | `make lint` | PASS — 92 files formatted; ruff, mypy (69 files), basedpyright clean |
| Unit/integration | `make test` | PASS — 752 passed, 18 skipped, 1 deselected |
| Evaluation notebook | `make eval` | PASS — 119/120 cases passed, 1 expected skip (DreaMS weights); 5.59 s |

Core-environment skips include the optional RDKit conversion test, imaging
backend tests and `scipy` for an optional Spearman reference. Actual RDKit
behavior is independently covered by the optional-chem run above; none is a core
dependency.

## Distribution qualification

Built from the candidate:

| Artifact | SHA-256 |
|---|---|
| `msmcp-1.0.2-py3-none-any.whl` | `95460d9b58278816e7547846533908f90e01f16afaca86893469356f0635f1d0` |
| `msmcp-1.0.2.tar.gz` | `dcc7cba107ff683d04d7c92e7af749e2860062190c20c43790f738be632acdd4` |

Verified independently for wheel and sdist:

- Fresh Python 3.13.12 virtual environments outside the checkout.
- `msmcp==1.0.2` installed; `massflow` **not** installed.
- Core install resolves `mcp 2.3.0`, `numpy 2.5.4`, `pydantic 2.14.1`.
- `msmcp` entry point starts with `MSMCP-MassFlow-Adapter v1.0.2`.
- Wheel metadata lists `massflow` only under the `imaging` extra and `rdkit`
  only under `chem`.
- `uv sync` / `uv run msmcp` from a source checkout remains the documented path
  until the package is published.

## Real MCP client evidence

`validation/installed_mcp_qualification.py` was run against the installed wheel
with the MCP Python SDK (`mcp 2.3.0`) over stdio. Evidence:

- Initialization succeeded: server name `MSMCP-MassFlow-Adapter`, version `1.0.2`,
  protocol `2025-11-25`.
- `tools/list` returned all 15 tools.
- `ping` succeeded and reported the optional imaging backend absent without
  claiming an error.
- `generate_qc_summary` read a real MGF query and reported bounded coverage.
- `load_spectrum` returned structured metadata, a `ptr:spectrum:` reference,
  source digest and software versions.
- `search_library` — `check_search_status` completed a real MSP library search:
  Caffeine ranked first at `1.0000`, the 900-precursor and wrong-polarity records
  were excluded before ranking, and the missing-charge record stayed labelled
  `unknown`.
- The report named the library format, spectrum count, digest, scorer, applied
  precursor tolerance, context-eligibility counts and provenance.
- No p/q column or significance filter appeared.
- Structured errors returned `is_error=true` for an out-of-root path, a malformed
  MSP library, an MS1 query and an out-of-bound spectrum index; the session
  remained healthy afterwards, which also demonstrates stdio purity.
- The MCP SDK masks exception detail in the wire error (`Error executing tool …`);
  the detailed diagnosis is on stderr. This is documented in the README and is a
  known diagnostic-UX limitation, not a silent success.

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

## Remaining blockers and risks

1. **Approval gate.** No merge, tag, PyPI upload or site deployment is authorised.
2. **PyPI pending publisher.** The invalid-publisher error cannot be fixed from
   the repository; the account-level publisher above must be created by the owner.
3. **Diagnostic detail over MCP.** The SDK masks exception text in `is_error`
   results. Structured failure status is visible, but the exact reason is only on
   stderr. This should be addressed in a follow-up if clients need the text.
4. **Scientific scope.** Search is similarity-ranked, not a calibrated
   identification or FDR procedure. Context gating depends on metadata the source
   declares; missing context is labelled unknown rather than inferred.
5. **Optional capabilities.** imzML, real embedding weights, remote transport and
   durable jobs are not qualified by this candidate. GitHub Dependabot alerts on
   the default branch include advisories in optional-extras chains (`rdkit`/
   `torch`/`massflow`); the core installed wheel has none of those packages. The
   candidate does not claim a dependency-security audit beyond the core install.
6. **Website deploy trigger.** Merging the site branch to `main` triggers the
   existing Pages workflow by design; do not merge until the owner approves the
   deployment.

## Recommendation

**GO for the core 1.0.2 candidate, conditional on owner approval.** The core
server installs cleanly, has no unintended imaging dependency, passes the full
gate, and completes a genuine installed-client workflow with bounded errors and
provenance. The website is consistent with the candidate and passes its static
gate.

**NO-GO for publishing or deployment** until the owner (a) approves the
integration PRs, (b) creates the PyPI pending publisher, and (c) authorises the
site merge. Those are explicit approvals, not follow-up work performed here.

## Draft review artefacts

- MSMCP draft PR: https://github.com/janusson/MSMCP/pull/82
  - CI run 40430031270: `gate` passed in 1m06s
- Site draft PR: https://github.com/janusson/msmcp-site/pull/3
  - CI run 40430041353: `Gate` passed (16s), `Build` passed (21s), `Deploy`
    skipped as designed for a pull request
- Site deployment checklist: `SITE_DEPLOYMENT_CHECKLIST.md` on the site branch

## Stop point

This candidate is proposed for review only. No automatic merge, release, tag,
PyPI upload or website deployment has been performed.
