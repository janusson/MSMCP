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
  upper-tail null, must land before any release that includes it. So `main` is ahead of the released
  version by design, and the next release is not a formality.
- **What the reader can do today.** It opens an MSP/NIST-style library (`.msp`, `.msp.gz`) through
  the same security boundary as an acquisition and searches it for real. It reads the
  `m/z:intensity` spelling the Fiehn/Golm GMD exports use, skips and counts metadata-only records
  rather than failing, and refuses an empty query spectrum before dispatch. Verified against
  `GNPS-LIBRARY.msp` (15,749 records), a 96-library collection, and a real Bruker maXis acquisition.
- **What it cannot do.** It cannot express a q-value below the sampled floor, and its cost is linear
  in library size.

---

## v1.1 — ship the library reader with a null model that can be trusted

### Why this milestone exists

The reader works. What it does not have is a statistical floor that holds at library scale: the null
scores 40 decoys per library spectrum (`NULL_DECOY_MULTIPLIER` in `src/msmcp/tools/search.py`),
which is linear in library size — 2.9 s at 2,200 spectra, ~130 s at 10⁵ — and floors the attainable
q at ≈0.025. "Searches a real library" and "reports a trustworthy significance" are different
claims, and only the first is true today.

The milestone also completes the reader's format coverage and adds the acquisition context that
makes a match interpretable.

### Acceptance criteria

The release is complete when the repository demonstrates all of the following.

**The null model (`#12`)**

- [ ] The upper tail is **fitted, not sampled**: a model is fitted to the decoy score distribution
      and evaluated analytically, so a q-value below the sampled floor is expressible and per-query
      cost is no longer linear in library size.
- [ ] A search report **names the null model it used and prints the smallest attainable q**, so a
      sampling floor can never be read as a result.
- [ ] The fitted null is **validated against the sampled one** on a library where both can be
      computed, with the disagreement quantified rather than asserted.

**Format coverage (`#2`)**

- [ ] `get_library_provider` resolves `.mgf`/`.mgf.gz` and a documented local SQLite peak store, in
      addition to MSP/NIST text.
- [ ] Each new provider crosses the **same `SecurityPolicy`** as MSP — allowed root with symlinks
      resolved, file-size limit, read-only — and is **probed before dispatch**, so a malformed
      library of any format is a typed tool error rather than a failed background job.
- [ ] The SQLite store is opened **read-only by URI** (`file:...?mode=ro`), with extension loading
      disabled and `ATTACH` refused. It is a binary format parsed by a library rather than by our own
      line reader, which makes it a different surface from MSP and not merely a third suffix.
- [ ] The SQLite schema is documented in `ARCHITECTURE.md`, including how a user builds a store from
      a text library.
- [ ] Banner and `Provenance` name what was opened for every format: path, format, spectrum count,
      digest.

**Acquisition context (`#16`)**

The point of this item, in the owner's words: *preserving how something was acquired matters as much
as the spectrum itself, because source settings can be manipulated to produce wildly different
results against a library spectrum. A good match should be obtained under comparable settings — but
that is often difficult to obtain.* The criterion is therefore about making comparability
**visible**, not about asserting it.

- [ ] A spectrum or search result reports the acquisition parameters that bear on comparability,
      **parsed from the file and nothing invented**: ionisation mode, polarity, MS level, collision
      energy, isolation window and target, analyser type and resolution, and source conditions where
      the file actually carries them.
- [ ] **Absence is visible and never implied away.** A spectrum or library entry whose file does not
      carry a parameter reports it as absent; no result implies that a query and a library entry
      were acquired under comparable settings when that is not known.
- [ ] The parameters are recorded in `Provenance` for the result that used them, following the
      pattern the instrument-class constants already use.

**Release**

- [ ] A release containing the reader is tagged. The `CHANGELOG`'s `[1.0.0]` section stays
      byte-identical to the tag; all new work sits under a new version heading.
- [ ] `ARCHITECTURE.md`'s *Known limitations* reflects what is real: which formats are genuinely
      readable, and what the null does at library scale.

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

## Open question the owner has not yet decided

Whether the `v1.1` criteria above should be promoted into `ARCHITECTURE.md` as a
`## Acceptance criteria for v1.1` section, beside the v1.0 block. Until they are, this document holds
them and `ARCHITECTURE.md` still describes v1.0 only. The version stays `1.0.0` until a release
includes the reader.

---

## Source material

- `docs/audit/2026-09-22-standard-audit.md` — the dated audit, findings F1–F13 and the 16-item
  backlog. Read it as a record of reasoning, not as documentation of the present state.
- `ARCHITECTURE.md` — what the software is, and the v1.0 acceptance criteria it met.
- `CHANGELOG.md` — what each release contains. Released sections are historical; never edit one.
