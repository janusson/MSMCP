# MSMCP 1.0.2 — final scientific and engineering qualification

**Assessment date:** 2026-10-31  
**Decision:** **NO-GO for publication of the currently hosted PR #82 head; conditional path to a scoped 1.0.2 release after the local P1 corrections below are reviewed and integrated.**  
**No merge, push, tag, publication, workflow dispatch, or deployment was performed.**

## 1. Candidate and worktree identity

| Item | Verified value |
|---|---|
| Candidate | PR [#82](https://github.com/janusson/MSMCP/pull/82), open and draft |
| Candidate branch / exact head | `release/1.0.2-candidate` / `e847aef9ee7f27e6527afd2be9722afbb35a2db4` |
| Base | `main@18e183a5873d6622b0cd09432b4056d13b0f3499` |
| Live CI | `gate` check run `41103478114` succeeded on the candidate head; `get_check_runs` returned one successful run. The combined-status endpoint returned no status entries. |
| PR review state | Draft; no submitted reviews. The body still names `601718…` and run `40761662742`, so its evidence header is stale. |
| Qualification tree | `.worktrees/qualification-1.0.2-20261031`, branch `qualification/1.0.2-final-20261031`, based directly on `e847aef…`; all qualification fixes are local and are not on PR #82. |
| Research tree | `.worktrees/significance-restoration`, branch `research/significance-restoration-20261031`; research-only commit `3bdc8cb6e56c33ff1930bd5f941072cfb677b77d`, based on the same candidate SHA. |

The primary checkout and the unrelated dirty MassFlow worktree were not modified. The research branch is not part of the release qualification or production search path.

## 2. Release scope and scientific boundary

The candidate’s appropriate release scope is a **local stdio MCP server** providing validated chemistry calculations, acquisition/QC tools, and similarity-ranked search with explicit context eligibility and provenance. Production search remains `msmcp.search.similarity.v1`: it reports candidate scores/ranks, not calibrated identification probabilities, p/q-values, or FDR control. Imaging and learned-model extras are optional and were not qualified as core functionality.

The qualification confirms no experimental significance inference. See the separate research status below. Absence of calibrated inference is not a blocker for this similarity-only release; advertising error-rate or identity-confidence claims would be.

## 3. Confirmed defects and local corrections

These defects were reproduced against the candidate behavior and corrected only in the isolated qualification tree:

| Severity | Reproduction / consequence | Local correction and evidence |
|---|---|---|
| **P1 — package isolation/reproducibility** | Hatchling’s sdist included ignored `.cache` build artifacts despite the repository `.gitignore`; repeat builds changed with local cache contents. No secret was confirmed in an artifact, but the behavior could publish unreviewed local cache data. | Explicit sdist exclusions now cover `.cache`, worktrees, environments, local build/evaluation output, release-qualification docs, credentials, and the qualification-only protocol/performance scripts. A regression test checks these exclusions. Final repeated sdists contain none of the excluded paths. |
| **P1 — invalid scoring domain** | `ClassicalScorer` returned `1.0` for matching peaks at m/z `0` and `-100`, allowing physically invalid coordinates to produce a perfect cosine. | Scoring rejects non-positive m/z; input docs and search docs state the finite-positive m/z and finite-nonnegative intensity domain. Tests cover zero/negative m/z through scorer and public-tool boundaries. Raw acquisition ingestion semantics are unchanged. |
| **P1 — isotope numerical/resource boundary** | Formula work was uncapped (`C100000` parsing took about 0.157 s in the reproduced case); absolute monoisotopic probability underflow made `Se1000` raise `ZeroDivisionError`. | Formula identifiers are capped at 4,096 characters and 10,000 total atoms; convolution uses probabilities relative to the monoisotopic abundance and checks finite arithmetic. Regression coverage verifies bounded rejection and finite selenium-rich output. |
| **P1 — precursor input finiteness** | Positive-infinity/NaN values are not valid exact masses and can make tolerance arithmetic meaningless. | `validate_precursor` now rejects non-finite as well as non-positive theoretical/experimental masses; parameterized boundary tests cover NaN and both infinities. |
| **P1 — vulnerable declared MCP floor** | The candidate lock resolved MCP 2.3.0, but `pyproject.toml` permitted MCP `<2.2.0`, within the open advisory’s vulnerable range. The advisory concerns bearer-token server authentication; MSMCP’s advertised transport is stdio. | Raised the minimum to `mcp[cli]>=2.2.0`, the first patched version. An exact 2.2.0 install of the final wheel passed the installed-client workflow. The lock resolves 2.3.0. |
| **P1 — decompression budget checked after allocation** | Before correction, a deterministic 64 KiB in-memory stream with an 8-byte budget showed underlying requests of `-1`, `-1`, and `65,536` bytes for `read`, `read1`, and `readinto`; the limit exception came only after those reads. This confirms the wrapper could materialize more than its configured limit before refusing. | All three methods now limit the underlying request to the remaining byte budget plus one sentinel byte; accounting rejects the sentinel before `readinto` copies data. Regression tests assert each request is 9 bytes, the source cursor is 9, and ordinary 4-byte sized reads remain unchanged. `tests/test_security.py` passes 32 tests. |

**Disposition:** no confirmed P0 remains in the corrected local snapshot. The P1 defects remain unresolved on the hosted PR #82 head because no qualification commit has been integrated there. This is the release stop condition.

## 4. Scientific qualification results

### Chemistry and numerical behavior

The existing chemistry tests use independent test-local NIST/CIAAW neutral atomic-mass and CODATA electron-mass references rather than reusing production constants. All 14 canonical adducts, signed electron bookkeeping, charge division, and representative multiply charged/negative-ion cases are covered by the candidate test suite. Formula parser rejection and isotope envelope tests also pass. The final core run skipped the one RDKit-enabled SMILES test because RDKit is optional and not installed; optional chemistry was not re-run in this qualification.

The MassBank ranking comparison below tests scorer behavior, not independent chemical identity confidence. No new experimental significance calibration was performed.

### Independent spectral scoring comparison

The qualification reran the pinned MassBank-data `2026.03` slice and `matchms==0.33.1` comparison. Dataset asset: `MassBank_NISTformat.msp`, 137,309,196 bytes, CC-BY-4.0, DOI `10.5281/zenodo.3378723`, SHA-256 `cf49e1b293f1c8c1b4a645123638826eea70db656bd76e09ea0c47a1a56fc269`.

On 250 positive-mode in-library queries and 3,499 references, MSMCP and matchms had median absolute score difference 0, p95 `1.809e-4`, maximum `0.4236`, and tie-aware Spearman `0.999459`. The nearest-m/z query-order matcher and matchms intensity-product matcher selected different top-1 reference records on 11/250 queries and different top-1 compounds on 6/250. Top-1 known-compound hit status disagreed on 6 queries (three scorer-only hits each); naive top-5 hit status disagreed on none, naive top-10 on two, and strict top-5 on one.

These are descriptive differences on one selected retrieval slice. The corpus has no acquisition-batch grouping in this analysis; it does not estimate open-set accuracy, FDR, or cross-instrument performance. No scorer was replaced. The result command was `uv run --with "matchms==0.33.1" python validation/similarity_validation.py --data .cache/massbank/MassBank_NISTformat.msp --out validation/massbank-2026.03-ranking-qualification.json`. The raw result and updated reproduction notes are in `validation/massbank-2026.03-ranking-qualification.json` and `VALIDATION_SIMILARITY.md`.

### Acquisition formats

The test suite covers mzML, MGF, MSP and SQLite parsing/provider behavior, including unit accessions, array pairing, missing metadata, malformed inputs and configured limits. The installed-client qualification used independently generated synthetic mzML/MGF/MSP fixtures. MassBank MSP was a real external library input in the similarity and performance runs. No real independent mzML acquisition or real MGF acquisition was available for this qualification; therefore parser regression tests are not described as experimental acquisition validation.

## 5. Required source gates

Final run in the qualification worktree, macOS arm64, Python 3.13.12, pytest 9.1.1, using the worktree `.venv` after `uv sync --extra dev --locked`:

| Exact command | Result |
|---|---|
| `uv sync --extra dev --locked` | PASS; locked environment audited, no lock drift |
| `make lint` | PASS; 96 Python files formatted; Ruff clean; mypy clean on 71 source files; basedpyright 0 errors / 0 warnings / 0 notes |
| `make test` | **772 passed, 18 skipped, 1 deselected** in 9.12 s |
| `uv run pytest -q tests/test_security.py` | PASS; 32 passed, including three read-budget methods and sized-read preservation |
| `make eval` | PASS; 119/120 notebook cases passed, one optional real-DreaMS case skipped; wrapper 1 passed / 1 deselected |

The 18 unit skips were 16 optional imaging cases, one RDKit case, and one optional SciPy reference. The eval skip requires optional DreaMS weights. None of these skips is reported as a pass or as validation of an optional extra.

## 6. Installed package and MCP stdio qualification

Both final distributions were built and installed into fresh environments outside the checkout using CPython 3.13.12. Installed metadata reported version `1.0.2`, entry point `msmcp.server:main`, and MCP 2.3.0. Core-only installs did not contain `massflow`, RDKit, Torch, or matchms. A separate exact MCP 2.2.0 environment also passed.

`validation/release_qualification_client.py` launched the installed executable as a real stdio server and used the MCP SDK client. Exact final-artifact commands (run from the qualification worktree) were:

```sh
/tmp/msmcp-release-qualification-20261031/wheel-package-final/bin/python validation/release_qualification_client.py \\
  --server-command /tmp/msmcp-release-qualification-20261031/wheel-package-final/bin/msmcp
/tmp/msmcp-release-qualification-20261031/sdist-package-final/bin/python validation/release_qualification_client.py \\
  --server-command /tmp/msmcp-release-qualification-20261031/sdist-package-final/bin/msmcp
/tmp/msmcp-release-qualification-20261031/mcp-2.2-min/bin/python validation/release_qualification_client.py \\
  --server-command /tmp/msmcp-release-qualification-20261031/mcp-2.2-min/bin/msmcp
```

The exact-MCP-floor environment was first changed to `mcp[cli]==2.2.0` with `uv pip install --python /tmp/msmcp-release-qualification-20261031/mcp-2.2-min/bin/python 'mcp[cli]==2.2.0'`; the final wheel was then installed into that environment. For each final wheel and sdist, the client:

- initialized and closed a protocol session;
- discovered all 15 core tools and confirmed their input schemas (plus the structured chromatogram output schema);
- exercised all 15 tools, including synthetic mzML/MGF/MSP ingestion, structured chromatogram extraction, reference creation/dereference/release, chemistry, cosine scoring/QC, and async search dispatch/polling;
- verified a real-library fixture result remains labelled uncalibrated, and that references do not expand into raw peak arrays;
- cancelled a dispatched 50,000-entry synthetic-library job; and
- verified malformed-library, MS1-query, out-of-root, symlink-escape and spectrum-budget errors while the session remained usable.

All three tested combinations (wheel/MCP 2.3.0, sdist/MCP 2.3.0, final wheel/MCP 2.2.0) passed. The protocol client’s stdout was valid JSON evidence; the server emitted expected exception tracebacks to stderr for deliberately invalid requests. Those stderr traces include local absolute paths and allowed-root details. This is a **P2 local-log disclosure/diagnostic concern**, not an access-control bypass observed in this test.

Reference release was verified over MCP. Job cancellation was verified over MCP. Job expiry was **not** waited out over the installed protocol because production TTL is one hour and has no test-only environment override; executor unit tests cover fake-clock sweeping and automatic short-TTL expiry. Mark installed-protocol job expiry NOT VERIFIED.

## 7. Build, package and publisher qualification

Two final builds from the qualification worktree used `uv build --out-dir /tmp/msmcp-release-qualification-20261031/build-g` and `uv build --out-dir /tmp/msmcp-release-qualification-20261031/build-h`; `shasum -a 256` confirmed identical artifacts:

```sh
uv build --out-dir /tmp/msmcp-release-qualification-20261031/build-g
uv build --out-dir /tmp/msmcp-release-qualification-20261031/build-h
shasum -a 256 /tmp/msmcp-release-qualification-20261031/build-g/msmcp-1.0.2.tar.gz /tmp/msmcp-release-qualification-20261031/build-g/msmcp-1.0.2-py3-none-any.whl /tmp/msmcp-release-qualification-20261031/build-h/msmcp-1.0.2.tar.gz /tmp/msmcp-release-qualification-20261031/build-h/msmcp-1.0.2-py3-none-any.whl
```

Fresh installs used these exact commands and both resolved MCP 2.3.0:

```sh
uv venv --python 3.13 /tmp/msmcp-release-qualification-20261031/wheel-package-final
uv pip install --python /tmp/msmcp-release-qualification-20261031/wheel-package-final/bin/python /tmp/msmcp-release-qualification-20261031/build-g/msmcp-1.0.2-py3-none-any.whl
uv venv --python 3.13 /tmp/msmcp-release-qualification-20261031/sdist-package-final
uv pip install --python /tmp/msmcp-release-qualification-20261031/sdist-package-final/bin/python /tmp/msmcp-release-qualification-20261031/build-g/msmcp-1.0.2.tar.gz
```

The builds produced:

| Artifact | SHA-256 |
|---|---|
| `msmcp-1.0.2-py3-none-any.whl` | `92500b39002a4676395f9264fa952bab55bd6e32755952eb1ad8ac679c6c9bdf` |
| `msmcp-1.0.2.tar.gz` | `ca28702d6efae8eb8021233ab27c88923ebf9b346ead4a85d93fcfced8b3916e` |

The sdist had 117 members, included README/CHANGELOG and the validation result referenced by included documentation, and excluded `.cache`, `.worktrees`, `.venv`, release-qualification docs/results, the evaluation output and qualification-only client/performance scripts. The wheel had 35 members. Both wheel and sdist installed and passed the real MCP workflow. The final project requirement is Python `>=3.13`; only Python 3.13.12 was tested here.

`pyproject.toml` and `uv.lock` declare/reproduce MSMCP 1.0.2. `CITATION.cff` identifies the MIT project and does not declare a conflicting version. `publish.yml` uses PyPI Trusted Publishing (OIDC), the `pypi` GitHub environment, and version-tag/explicit-dispatch triggers; no publishing workflow was triggered. Account-side publisher registration and GitHub environment protection are not observable from source and remain owner checks. The previously recorded 1.0.0 upload attempt failed with `invalid-publisher`; that was not a 1.0.2 upload.

GitHub Dependabot’s live default-branch snapshot during this review had 59 open alerts (5 critical, 24 high, 27 medium, 3 low). This is **main’s** alert set, not a finding that the candidate lock has those vulnerable versions. Candidate lock versions include MCP 2.3.0, filelock 4.2.4, Pillow 13.0.0, opentelemetry-api 1.45.1, setuptools 84.2.1, PyJWT 2.15.0 and httpx2 2.12.0, at or beyond the applicable first-patched versions returned by the alerts. The minimum MCP floor was tightened as above. Optional `[imaging]`, `[chem]` and `[ml]` extras were not installed or comprehensively audited; the clean core install is not an extras audit.

## 8. Bounded performance result

Reproduction command, with the final installed wheel and the pinned MassBank asset:

```sh
/tmp/msmcp-release-qualification-20261031/wheel-qualification-final/bin/python validation/release_performance_probe.py \\
  --server-command /tmp/msmcp-release-qualification-20261031/wheel-qualification-final/bin/msmcp \\
  --library .cache/massbank/MassBank_NISTformat.msp \\
  --sha256 cf49e1b293f1c8c1b4a645123638826eea70db656bd76e09ea0c47a1a56fc269
```

The harness starts the installed server over stdio and scans the entire 139,006-spectrum, 137,309,196-byte MSP with chunk size 2,000 using a fixed **synthetic** five-peak query. On the final wheel after the `_BoundedStream` fix, observed: 12.694 s from client start through report, 4,324-byte report, and 185,417,728 bytes maximum child RSS on macOS (Darwin `ru_maxrss` bytes). The complete machine result is `docs/release-qualification/PERFORMANCE_RECEIPT.json`. This is one bounded workload, not an acceptance threshold, throughput guarantee, or identification result. Repeated-job stability, memory retained after release, and cancellation latency under representative large jobs were not characterized.

## 9. MassPilot interoperability

The current clean consumer checkout inspected was MassPilot `89ae43b6a73d8e8dc26b0cfa0256906a4d1063c1`. Its investigation code explicitly states it has no concrete MSMCP adapter for structured acquisition metadata/QC or MS/MS evidence; its consumer-side contract is aspirational and uses synthetic typed fixtures. Therefore there is **no current end-to-end MassPilot↔MSMCP integration to certify**. MSMCP’s existing MassFlow contract probe and tests are synthetic software-contract checks; the dated 2026-10-21 probe is not a current MassPilot run. No MassPilot dependency environment or files were modified.

Classify this as **P2 / separate integration work**, not a blocker to a release that does not claim MassPilot interoperability. Do not claim that MassPilot currently consumes the 1.0.2 MCP output.

## 10. Significance research status

The research-only commit is `3bdc8cb6e56c33ff1930bd5f941072cfb677b77d`. It adds the method audit, preregistration protocol, restoration plan, corpus-manifest template, reproducibility receipt, and an offline deterministic benchmark/test scaffold. The final research run was `make lint && make test && make eval`: 773 passed, 18 skipped, 1 deselected; eval 119/120 with the optional DreaMS skip. The seven artifact hashes in `REPRODUCIBILITY_RECEIPT.md` match the committed research files.

No independent experimental significance corpus was available. The MassBank similarity corpus is not wrong-target null calibration and lacks the batch grouping needed here. The scaffold’s synthetic tests validate software behavior only. Decoy permutations, GPD tails, nominal permutation estimates and historical BH diagnostics remain research comparators; production inference is not restored and no p/q/FDR claim is validated.

## 11. PR/source reconciliation

| PR | Live status / candidate relationship | Disposition |
|---|---|---|
| #72 significance withdrawal | Open/unmerged. Its withdrawal is present in #82 ancestry via merge commit `4260c03415e10196903f71e49c5b85b61ce24478`; production search remains similarity-only. | Do not merge separately or add research inference. Keep its boundary. |
| #81 mzML reader integrity | Open source PR; candidate contains strict unit/paired-array behavior and tests. | Integrated in #82; no double merge. |
| #83 acquisition-context eligibility | Open source PR; candidate implements issue #79 eligibility and has focused tests. | Do not double merge. |
| #84 SQLite schema documentation | Open source PR; candidate readiness ledger records the schema/coverage documentation. | Candidate duplicate; owner may close/reconcile after integration. |
| #85 library format coverage | Open source PR adds `library_path` to provenance parameters and docs/tests. Candidate already records library path as a `SourceRef`, format/digest in provenance parameters, and names the library in the report. | Semantically covered for traceability; the extra scalar path is redundant, not a release blocker. Do not merge wholesale. |
| #62 MGF library provider | Open; candidate provider registry supports MGF (and SQLite/MSP). | Superseded by candidate implementation; no double merge. |
| #61 Numpress / #66 demo | Open/optional work not in the supported core scope. | Defer; no release claim. |
| #63/#75 and #60/#68 | Candidate contains bounded chromatogram, interoperability-contract and documentation/citation work per its ledger. | Do not double merge. Current MassPilot adapter remains absent. |
| #65/#74/#76/#77/#78 | Source dependency PRs remain open; candidate lock contains updated dependency versions. | Do not double merge; refresh default-branch alert status after any approved integration. |

The source list is not an authorization to close or merge those PRs. PR #82’s stale body and readiness receipts must be refreshed at owner review.

## 12. Findings, decision and integration path

- **P0:** none confirmed in the corrected local snapshot.
- **P1:** the cache-inclusive sdist, invalid non-positive scoring coordinates, isotope underflow/unbounded work, non-finite precursor inputs, vulnerable MCP dependency floor, and decompression read-before-budget behavior were corrected locally. These remain unresolved in the exact hosted PR #82 head until this qualification diff is integrated.
- **P2:** expected tool-exception tracebacks disclose local paths on stderr; installed-protocol job expiry was not waited out; runtime outbound network behavior was not instrumented; optional extras are not qualified; there is no current MassPilot adapter; no independent real mzML acquisition or broad performance/stability campaign is available. Do not claim network isolation from this qualification.
- **P3:** further cross-instrument/mode scorer evaluation and optional-extra qualification are future work, not 1.0.2 scope.

**Release decision:** the current hosted PR #82 at `e847aef…` is **NO-GO for publication** because the reproducible P1 corrections above are not present in that head. The corrected local snapshot passed its required source gates, repeated builds, fresh wheel/sdist installs, and installed stdio tests. After the local qualification commit is reviewed and integrated, the scoped similarity-only release is a **CONDITIONAL GO**, subject to hosted CI/artifact re-verification and owner approval. Future statistical inference and actual MassPilot integration are not release blockers for the stated scope.

**Integration status and sequence:** this authorized follow-up branch is the review vehicle; the hosted PR #82 branch has not been modified or merged.

1. Review this draft PR targeting `release/1.0.2-candidate`; do not merge #72 or #85 separately and do not rebase the shared candidate branch.
2. Reconcile edits to `pyproject.toml`, `uv.lock`, `CHANGELOG.md`, `README.md`, scoring/chemistry/security tools and tests if the candidate moves. Historical #72 integration conflicted in `CHANGELOG.md`, `README.md` and `src/msmcp/tools/search.py`; those paths deserve manual review even though this branch is based on the exact current head.
3. On any eventual integrated head, rerun hosted CI, repeat the two-build artifact hash/content inspection and fresh installed-client checks. Refresh the stale PR #82 head/check-run/readiness text and obtain explicit owner review.
4. Separately, the owner verifies PyPI pending Trusted Publisher (`msmcp` / `janusson` / `MSMCP` / `publish.yml` / environment `pypi`) and GitHub environment protection before any separately authorized tag or publication. Nothing in this qualification performs those actions.
