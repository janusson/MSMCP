# Eric's capture runbook

**READY FOR ERIC TO CAPTURE — verified computation and replay; capture pending
on a human.** The reproducible stdio replay is the computational deliverable.
The following procedure uses the existing checkout and existing Zed model
connection. It requires no choice of dataset, scan, formula, adduct or tolerance.
Do not reuse earlier scan-9 numbers: the selected example is now scan 591.

1. **Prepare the environment.** In Terminal, paste this block:

   ```bash
   # Enter the existing PR worktree and sync its locked environment.
   cd "$HOME/Programming/msmcp-worktrees/prepublication-demo"
   uv sync --locked --extra dev
   # Create the destination for the human-captured PNG.
   mkdir -p docs/assets
   ```

2. **Prepare the public inputs.** Paste:

   ```bash
   # Verify existing input hashes, or download them if absent, and convert the measured library record.
   uv run python examples/public_demo.py prepare --data-dir /tmp/msmcp-public-demo
   ```

3. **Run the verified replay.** Paste:

   ```bash
   # Start the real stdio server and write a fresh transcript without overwriting an earlier run.
   uv run python examples/public_demo.py replay --data-dir /tmp/msmcp-public-demo \
     --output "$(mktemp -d /tmp/msmcp-capture.XXXXXX)/run"
   ```

   The command must exit successfully and report index **590**, native scan
   **591**, **18** peaks, `mass_consistent: false`, and
   `identification_established: false`. A rejected hypothesis is the expected
   successful demonstration outcome. The JSON/log files are evidence to inspect,
   not a conversation to paste into the client.

4. **Configure the real client.** Paste:

   ```bash
   # Print this checkout's exact Zed configuration; no path placeholders need replacing.
   uv run python examples/public_demo.py client-config --data-dir /tmp/msmcp-public-demo
   ```

   In Zed, run **`zed: open settings file`** from the Command Palette. Add the
   generated `msmcp-public-demo` entry inside the existing `context_servers`
   object; replace that entry if it already exists and retain other entries.
   If that object does not exist, add the generated
   `context_servers` object. Save and close Settings. In **Settings → AI → MCP
   Servers**, confirm **msmcp-public-demo** is active, then close Settings.
   This follows [Zed's local-server configuration](https://zed.dev/docs/ai/mcp).
   The generated command uses the same `serve` launcher verified by the replay:
   its working directory is the data directory, allowing basename-only inputs.
   The configuration contains local executable paths; never photograph it.

5. **Start a fresh real Agent conversation.** Keep the current connected model.
   Paste exactly this question; approve the requested read/calculation tools:

   > Can this later MS/MS precursor be protonated tryptophan? Use MSMCP to
   > summarize `32Mix_Phala_Tyr_1ugml_1.mzML` (two spectra), load index 590
   > (native scan 591), and summarize its returned reference. Call the formula
   > tool for C11H12N2O2 and the adduct tool for [M+H]+. Validate the actual
   > precursor with `instrument_class="orbitrap"`, using independently calculated
   > expected ion m/z 205.0971541013389. In a compact table show scan, stored
   > peaks, retention time, observed/expected m/z, signed ppm
   > `(observed-expected)/expected*1e6`, window and verdict. The tool's error is
   > absolute; label the signed calculation separately. Show only the source
   > basename and hash prefix, never paths or instrument serials. Explain what
   > this rejects and what remains unknown. Do not perform a library search.

6. **Verify the real tool history before arranging the picture.** These six
   calls must appear in this order (use each newly returned reference):

   | Tool | Required input / result |
   |---|---|
   | `load_mzml_summary` | Acquisition basename, `max_spectra=2`; 4,747 total spectra |
   | `load_spectrum` | Same basename, `spectrum_index=590`; 18 peaks, precursor 724.722412109375 |
   | `summarise_reference` | Reference returned immediately above; source hash matches |
   | `annotate_isotopes` | `identifier="C11H12N2O2"`, three isotopologues |
   | `predict_adduct_offset` | `adduct_string="[M+H]+"` |
   | `validate_precursor` | Expected 205.0971541013389, observed 724.722412109375, `instrument_class="orbitrap"`; **VALIDATION REJECTED** |

   The source hash must begin `7536385fb4ba7203`. Check the full value against
   [the provenance record](README.md). No peak list, instrument serial or
   embedded vendor source-file path is needed for the photograph. If a tool
   fails or a value differs, stop capture and retain the error for diagnosis;
   do not edit the answer or tool result to match this runbook.

7. **Arrange the actual client window at 1600 × 900 pixels.** Expand the Agent
   conversation to fill the window. The open pane is the **Agent conversation**,
   with **`validate_precursor` expanded inline** and its input/result visible.
   Keep the scientific question and the model's compact result table visible.
   Collapse the result bodies of `load_spectrum` and `summarise_reference`,
   which can contain absolute paths. Keep their tool-call headers in the history;
   they may be above the visible portion of the conversation. Close editor,
   terminal, Settings, raw XML and unrelated chat/project panes; hide sidebars.
   No second editor or plot pane is required.

   These values must be readable on screen, in the genuine result/interpretation:

   | Field | Required display |
   |---|---|
   | Native scan / zero-based index | **591 / 590** |
   | Stored peaks | **18** |
   | Retention time | **2.00177 min** (2.00 min in rounded tool text) |
   | Observed m/z | **724.722412** |
   | Expected [M+H]+ m/z | **205.097154** |
   | Signed error (assistant calculation) | **+2533556.65 ppm** |
   | Absolute error (tool) | **2533556.65 ppm** |
   | Orbitrap profile window | **5.0 ppm** |
   | Verdict | **VALIDATION REJECTED** |
   | Interpretation | Rejects this ion hypothesis; no compound identity or whole-run absence claim |
   | Provenance | Acquisition basename and `sha256:7536385fb4ba7203…` |

   These supersede the old scan-9 values in the earlier draft. A positive
   number is now also the signed error because observed m/z exceeds expected
   m/z. The tool does not report a signed error itself. Use the full-precision
   inputs for calculation; do not recalculate from six-decimal display values.

8. **Check privacy, then capture the window.** Nothing visible may contain an
   absolute path (including `/tmp` or `/private/tmp`), the embedded vendor
   source-file location, an instrument serial, any credential, private files or
   unrelated conversation. Fix visibility by collapsing/closing panes; never
   rewrite tool text. Use macOS **Shift–Command–4, then Space**, and click the
   arranged Zed window. Save the genuine image as
   `docs/assets/msmcp-real-mzml-precursor-check.png` in this checkout. Inspect
   it at full size and a 390-pixel-wide preview: the question, numeric result
   and interpretation must remain legible. A framing crop may be disclosed;
   do not splice, redraw or generate conversation/result content.

9. **Use this caption and alt text only once the image matches.**

   **Caption:** A real MS/MS acquisition and a hypothesis that does not survive
   the numbers. MSMCP reads scan 591, keeps 18 peaks server-side, and rejects
   protonated tryptophan at the Orbitrap profile's 5 ppm window. The tools
   calculate; the agent explains the evidence and its limits. This does not
   identify the observed ion. Data: G. A. Vitale, Zenodo
   10.5281/zenodo.10043877, CC BY 4.0; derived summaries and calculations.

   **Alt text:** Real AI-client conversation showing MSMCP scan 591 with 18
   stored peaks and a rejected protonated-tryptophan hypothesis: observed m/z
   724.722412, expected 205.097154, signed error +2533556.65 ppm against a
   5.0 ppm Orbitrap window, with a statement that the selected ion remains
   unidentified.

The human capture is pending on Eric. No merge, release or public post is part
of this procedure.
