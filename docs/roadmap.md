# Roadmap

This is the maintained plan of record for MSMCP: where the repository is, what the next milestone
requires, and what is deliberately not being done. It replaced issue #11, which held the same
content in a place with no history, no diff and no review.

**Live ordering lives on the [MSMCP Core board](https://github.com/users/janusson/projects/9)** —
its `Plan` field carries `Now` → `Next` → `Later` → `Blocked upstream` → `Out of scope`. This
document is the narrative and the acceptance criteria; the board is the queue. Where they disagree,
the acceptance criteria here win, because they are the thing a release is judged against.

Nothing is deleted from either. Completed and abandoned work stays in the record.

---

## Where the repository is

- **`1.0.0` is released** — tag `a58e50a`, with a GitHub release. `main` is protected by a required
  `gate` status check (`ruff format --check`, `ruff check`, `mypy`, `basedpyright`, `pytest`,
  `make eval`) and forbids deletion and force-push.
- **The spectral-library reader is on `main` and deliberately unreleased.** `#12`, the fitted
  upper-tail null, was the release blocker; it has now landed on the working branch
  (`msmcp.models.null_model`), so a release that includes the reader is no longer waiting on
  statistics. The version still stays `1.0.0` until that release is cut — see *Version policy*.
- **What the reader can do today.** It opens an MSP/NIST-style library (`.msp`, `.msp.gz`) through
  the same security boundary as an acquisition and searches it for real. It reads the
  `m/z:intensity` spelling the Fiehn/Golm GMD exports use, skips and counts metadata-only records
  rather than failing, and refuses an empty query spectrum before dispatch. Verified against
  `GNPS-LIBRARY.msp` (15,749 records), a 96-library collection, and a real Bruker maXis acquisition.
- **What it cannot do.** The scan is exhaustive, so its cost stays linear in library size (the null
  is now constant). The fitted tail is a method-of-moments generalised-Pareto approximation, measured
  against the sampled p-values rather than exact, and the reader still covers only MSP/NIST text.

---

## v1.1 — ship the library reader with a null model that can be trusted

### Why this milestone exists

The reader worked, but its statistical floor did not hold at library scale: the null scored 40 decoys
per library spectrum (`NULL_DECOY_MULTIPLIER` in `src/msmcp/tools/search.py`), linear in library size
— 2.9 s at 2,200 spectra, ~130 s at 10⁵ — and floored the attainable q at ≈0.025, so "searches a real
library" and "reports a trustworthy significance" were different claims.

`#12` has now landed: `msmcp.models.null_model` fits the decoy upper tail and `search_library` reads
significance from it over a **fixed** decoy sample, so the sampled floor is gone and the null cost is
constant. What remains for v1.1 is the reader's format coverage and the acquisition context that
makes a match interpretable.

### Acceptance criteria

They live in [`ARCHITECTURE.md`](../ARCHITECTURE.md#acceptance-criteria-for-v11), directly after the
v1.0 criteria they follow. They are deliberately not repeated here: two copies of a contract drift,
and this repository has already been bitten by exactly that.

### Explicitly not v1.1

| Item | Why not |
|---|---|
| `#17` acquisition-aware confidence | Depends on `#16` and on real acquisitions. Sequenced next, not now. |
| `#18` calibration, alignment, comparability tools | Depends on `#17`. This is the "are two calibrations comparable" question, and it is a milestone of its own. |
| `#8` MS-Numpress mzML arrays | Documented limitation with an opt-in extra; no criterion references it. |
| `#3` whole-slide imzML benchmark | Measurement, not capability. |
| `#6` durable job execution | `ARCHITECTURE.md` files it under *Future extension (not v1.0)*; `JobExecutor` is the seam. |
| `#19` distribution: `server.json`, PyPI, container | Adoption infrastructure, not correctness. |

---

## Scientific validation

Where the repository makes *scientific* claims, it records the evidence for them
separately from the software tests — a passing suite says the code matches its
tests, not that the science is right.

| Capability | Status | Evidence |
|---|---|---|
| Exact mass / isotope envelope; mzML ingestion | **VALIDATED** (independent references) | `SCIENTIFIC_VALIDATION.md` §3, §4 |
| Spectral similarity / library search | **PARTIAL** — independently compared, recovery measured, limits stated | `VALIDATION_SIMILARITY.md` |
| Statistical significance / FDR (`#12`, `#51`) | **NOT VALIDATED** | `SCIENTIFIC_VALIDATION.md` §6 |

`#54` (independent similarity / library-search validation) is **complete**:
MSMCP's `ClassicalScorer` was compared against `matchms` `CosineGreedy` on a
pinned MassBank release (release, DOI and SHA-256 fixed; the dataset stays
outside the repository) and held-out library recovery was measured. It does
**not** establish identification confidence, a calibrated FDR, or end-to-end
analytical correctness, and the greedy-matching discrepancy is documented, not
resolved. `#12`/`#51` (the search-null floor) stays a separate research problem
and the null model is unchanged until held-out decoy data exists.

The harness is opt-in (`make validate-similarity`); `matchms` is not a runtime
dependency.

---

## Waiting on someone else

Not work — conditions to watch. These cannot be started, and every agent session that sees them in a
todo column will re-propose them, which is why they are listed here instead.

| Issue | Waiting on |
|---|---|
| `#7` | the `mcp` SDK dispatching `tasks/*` |
| `#9` | upstream publishing public LSM-MS2 inference weights |
| `#10` | MassFlow shipping an mzML and/or MGF reader |

## Out of scope — preserved, closed as not planned

Kept so the reasoning is not lost. Each carries a full account of what the work would have involved
and what would have to change first.

| Issue | Why |
|---|---|
| `#20` online / cross-database connectors | Would break the offline, credential-free deployment posture. |
| `#21` authenticated remote MCP transport | stdio-only by design; anyone who can drive the host LLM can drive the server. |
| `#22` instrument control and hardware actuation | Belongs to a different project. The MCP layer must never be the only barrier between a model and physical hardware. |

## Version policy

The version stays `1.0.0` until a release includes the library reader. The v1.1 criteria are declared
in `ARCHITECTURE.md`; `#12` now meets its three, but the format-coverage and acquisition-context
criteria are still open, and nothing here bumps a version or cuts a release.

## Source material

- `docs/audit/2026-09-22-standard-audit.md` — the dated audit, findings F1–F13 and the 16-item
  backlog. Read it as a record of reasoning, not as documentation of the present state.
- `ARCHITECTURE.md` — what the software is, and the v1.0 acceptance criteria it met.
- `CHANGELOG.md` — what each release contains. Released sections are historical; never edit one.
