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
answer key, never sent to the server).

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
`workflow_report.json` with the raw payloads. Point it at a different entry
point with `--server /path/to/msmcp`.

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

The same seed on the same release produces the same files. The report echoes the
library's SHA-256, so a different subset is visible rather than implied.
