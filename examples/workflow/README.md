# One workflow, end to end: installation to interpretation

This directory runs a real spectral-library search and shows how to read its
result. Every spectrum is a **measured** spectrum from the pinned MassBank
release — unlike `examples/caffeine_*.{mgf,msp}`, which are explicitly fabricated
and exist only to exercise the search mechanics deterministically.

It takes about a minute, needs network once, and produces a report you are
expected to interpret rather than trust.

| Step | Command | What it proves |
|---|---|---|
| Install | `pip install msmcp==1.0.2` | the published artifact runs |
| Prepare | `python prepare_data.py --fetch` | the data is real and its provenance is checkable |
| Run | `python run_workflow.py` | QC, a spectrum reference, a context-gated search, a report |
| Re-run elsewhere | `python run_workflow.py --data DIR --out FILE` | the same workflow against data prepared outside the checkout |
| Interpret | read [`INTERPRETATION.md`](INTERPRETATION.md) | what the numbers license |

## 1. Install

```bash
python3.13 -m venv .venv
.venv/bin/pip install "msmcp==1.0.2"
.venv/bin/pip show msmcp | head -3        # expect Version: 1.0.2
```

## 2. Prepare the data

```bash
.venv/bin/python prepare_data.py --fetch
```

This downloads `MassBank_NISTformat.msp` (137 MB, MassBank release `2026.03`),
**verifies its SHA-256 before reading it**, and derives a small subset:

- filters mirror `VALIDATION_SIMILARITY.md` §Preprocessing — positive mode, MS2,
  5–300 peaks, precursor 100–1200 Da, InChIKey stated → 59,061 of 139,006 records;
- picks one compound measured more than once, holds out one measurement as the
  query, and keeps its *other* measurements as references;
- adds seeded distractor compounds, dropping any whose peak list duplicates the
  query (a duplicate would leak the answer).

Outputs into `data/`: `query.mgf`, `library.msp`, and `manifest.json` (source
URL, digest, licence, seed, counts, and the held-out compound's InChIKey — the
answer key, never sent to the server). Pass `--out DIR` to write the three files
somewhere else; `run_workflow.py --data DIR` then reads them from there.

The downloaded release is cached at `~/.cache/msmcp-workflow/` and is never
committed. To use a copy you already have:

```bash
.venv/bin/python prepare_data.py --source /path/to/MassBank_NISTformat.msp
```

## 3. Run the workflow

```bash
.venv/bin/python run_workflow.py
```

The client speaks MCP over stdio to the installed `msmcp` and runs:

1. `generate_qc_summary` — quality control on the query acquisition;
2. `load_spectrum` — a server-side reference (`ptr:spectrum:…`) plus provenance;
3. `search_library` — dispatches a job against the MSP library;
4. `check_search_status` — polls until the report is ready.

It prints the QC summary and the search report, and writes
`workflow_report.json` with the raw payloads. Three flags control what it reads
and writes:

| Flag | Default | Meaning |
|---|---|---|
| `--data` | `examples/workflow/data` | directory holding `query.mgf` and `library.msp`; it is also the `MSMCP_ALLOWED_ROOT` the server is confined to |
| `--out` | `examples/workflow/workflow_report.json` | file for the raw payloads; a missing parent directory is created |
| `--server` | `msmcp` | the entry point to launch, e.g. `--server /path/to/.venv/bin/msmcp` |

Both path defaults are inside this checkout and gitignored, so they are where
`prepare_data.py` writes by default. If you prepared the data elsewhere, pass
that directory to both steps:

```bash
.venv/bin/python prepare_data.py --fetch --out /tmp/workflow-data
.venv/bin/python run_workflow.py \
    --data /tmp/workflow-data --out /tmp/workflow-report.json
```

A run against the pinned release with the default seed prints, in this order:
the QC summary, a `REAL LIBRARY — READ FROM DISK` banner carrying the library's
SHA-256, and a `## Spectral Library Search Results` table. Recorded from a run
of `msmcp==1.0.2` against that subset, the context gate reports 9 matched /
0 unknown / 191 excluded, and the held-out compound is ranked first. Read those
as a software smoke check, not as an analytical validation: different counts
mean the subset, the server version or the report contract moved and are worth
investigating, while a matching run only shows that the plumbing worked.

## 4. Read the result

Open [`INTERPRETATION.md`](INTERPRETATION.md). The short version: the top of the
list is a similarity ranking under a precursor gate, not an identification, and
the report says so in its own banner.

## Connecting a host LLM instead of the script

Any stdio-capable host can run the same server. For Zed
(`.zed/settings.json`), with the package installed:

```json
{
  "context_servers": {
    "msmcp": {
      "command": "msmcp",
      "args": [],
      "env": {
        "MSMCP_ALLOWED_ROOT": "/absolute/path/to/examples/workflow/data"
      }
    }
  }
}
```

`MSMCP_ALLOWED_ROOT` is the boundary the server will not read outside of; set it
to the directory holding the files you want analysed.

## Reproducing exactly

| Quantity | Value |
|---|---|
| Dataset | MassBank `2026.03`, `MassBank_NISTformat.msp` |
| SHA-256 | `cf49e1b293f1c8c1b4a645123638826eea70db656bd76e09ea0c47a1a56fc269` |
| Licence | CC-BY-4.0 — attribution to the MassBank consortium required |
| DOI | `10.5281/zenodo.3378723` |
| Selection seed | `20261109` (change with `--seed`) |
| Subset | 200 references, one held-out query |
| Traceability | every reference carries its MassBank `DB#` accession; the held-out record's accession is in `manifest.json` |

The same seed on the same release produces the same files. The report echoes the
library's SHA-256, so a different subset is visible rather than implied.
