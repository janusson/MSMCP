# Plan: fix the stale Zed MCP launch schema (and guard it)

**Plan file:** `.hermes/plans/2026-10-07_144143-zed-mcp-config-schema.md`
**Repo:** `/Users/ericjanusson/Programming/msmcp` (GitHub `janusson/MSMCP`, default branch `main`)
**Author context:** written after read-only inspection on 2026-10-07; every file:line below was read, not recalled.

---

## Goal

Make the repository ship Zed's **current** `context_servers` schema in both `.zed/settings.json` and the README example, and add tests that fail if either the config, the docs, or the launch line drifts again.

---

## Current context / assumptions

### What the defect is

Zed's MCP settings schema for a local (stdio) server changed. The current form is a **`command` string plus an `args` array**:

```json
{ "context_servers": { "name": { "command": "uv", "args": ["run", "msmcp"], "env": {} } } }
```

The repository ships the **pre-2025-11-25** form, where `command` was an **object**:

```json
{ "context_servers": { "name": { "command": { "path": "uv", "args": ["run", "msmcp"] } } } }
```

Evidence (all observed this session):

| Evidence | Source |
|---|---|
| Current schema documented as `command` string + `args` | https://zed.dev/docs/ai/mcp |
| Zed ships `remove_context_server_source()` + a migration whose own test fixtures flatten `command: {path, args}` → `command` + `args` | `zed-industries/zed` → `crates/migrator/src/migrations/m_2025_11_25/settings.rs`, `crates/migrator/src/migrator.rs` (`test_flatten_context_server_command`) |
| Installed Zed is 1.22.0 (build 20260930) and its binary contains `2025-11-25` → the migration is present | `/Applications/Zed.app/Contents/MacOS/zed`, `defaults read …/Info.plist CFBundleShortVersionString` |

**Consequence:** the repo's config only works because Zed rewrites it on load. Whether that migration also applies to **project-local** `.zed/settings.json` (as opposed to the user's global settings) is **not verified** — so relying on it is not acceptable.

### State of the working tree right now (important)

| Path | State |
|---|---|
| `.zed/settings.json` | **Already fixed** in the working tree (uncommitted, `M`), but `HEAD` still contains the legacy form |
| `README.md:196-209` | **Still stale** — publishes the legacy block |
| `docs/mcp-server-configuration.md` | Contains the legacy form at lines 125, 140, 166, 233, 246, 323, 375 — **do NOT touch** (see Task 7) |
| `examples/*.html` | Contain no MCP config (`grep -c context_servers examples/*.html` → 0) |
| `ARCHITECTURE.md` | No MCP host config |

### Assumptions

1. The plan implementer has this repo checked out, `uv` on `PATH`, and no other uncommitted work besides the `.zed/settings.json` edit named above. **Verify with `git status --short` at Task 0 before doing anything.**
2. Zed is the only editor host configured in this repo (confirmed: only one `context_servers` block exists in README.md, at line 200).
3. Lint cannot catch this class of defect: `pyproject.toml:64` excludes `docs/mcp-server-configuration.md` from ruff, and README/JSON are not linted at all. Hence the explicit tests in Task 2.

---

## Architecture / proposed approach

Treat the shipped config as the single source of truth for the launch line, fix the two stale sites (`.zed/settings.json`, README), and add one small test module that (a) asserts the config uses the current schema and resolves to `uv run msmcp`, (b) asserts the README block parses equal to the shipped file, and (c) actually spawns the configured argv and drives an MCP `initialize` + `tools/list` handshake. No production code changes, no new dependencies, no refactors.

---

## Step-by-step tasks

Each task is 2–5 minutes. Commands are copy-pasteable and run from the repo root. **Expected output is stated for every command.**

### Task 0 — Establish the baseline gate (do this first)

```bash
cd /Users/ericjanusson/Programming/msmcp
git status --short
git log --oneline -5
```

- Expected `git status --short`: exactly `M .zed/settings.json`. If anything else appears, **stop and ask Eric** — there is other work in this tree.
- Expected `git log --oneline -5`: starts with `4a7ec82 Bump httpx2 from 2.10.0 to 2.12.0 (#1)`.

Then reproduce CI's step order verbatim (this repo's CI order differs from `make lint`; see `.github/workflows/ci.yml:39-55`):

```bash
uv run ruff format --check .
make lint
make test
make eval
```

- `uv run ruff format --check .` → expected `48 files already formatted`.
- `make lint` → expected `All checks passed!`, then `Success: no issues found in 42 source files`, then `0 errors, 0 warnings, 0 notes`.
- `make test` → expected `423 passed, 1 deselected` (about 37 s).
- `make eval` → the notebook run plus a per-tool roll-up. **Record its output and runtime.** If `make eval` already fails on the untouched tree, record that as the pre-existing baseline and do not attempt to fix it in this task.

### Task 1 — Save the pending fix as a patch, then revert the file (RED setup)

The `.zed/settings.json` fix is uncommitted. To prove the new tests detect the defect, put the file back to its committed (legacy) state — the patch keeps the fix recoverable.

```bash
cd /Users/ericjanusson/Programming/msmcp
git diff -- .zed/settings.json > /tmp/zed-fix.patch
git checkout -- .zed/settings.json
git status --short
cat /tmp/zed-fix.patch
```

- Expected `git status --short`: empty (clean tree).
- Expected `cat`: a diff removing the nested `"command": { "path": "uv", … }` block and adding `"command": "uv"`, `"args": ["run", "msmcp"]`, `"env": {}`.

If `/tmp/zed-fix.patch` is empty, `HEAD` already has the fix — skip the revert and adjust the expected RED result in Task 2 to "already passing; RED was demonstrated in the plan's evidence section". Do not fabricate a failure.

### Task 2 — Add the regression tests (TDD: must fail now)

Create `tests/test_zed_config.py` with exactly this content:

```python
"""Guards for the editor-facing MCP launch configuration.

``.zed/settings.json`` is the exact launch line Zed runs when it spawns MSMCP
as a child process, and ``README.md`` publishes the same block for other
hosts.  Both must use Zed's current ``command``/``args`` schema: the nested
``command: {"path": ..., "args": ...}`` object is the pre-2025-11-25 form and
is rewritten by Zed's settings migration on load, so a repo that ships it
depends on undocumented migration behaviour.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
ZED_SETTINGS = REPO_ROOT / ".zed" / "settings.json"
README = REPO_ROOT / "README.md"

EXPECTED_ARGV = ["uv", "run", "msmcp"]


def _json_objects_in_fences(text: str) -> list[dict[str, Any]]:
    """Return every ``json`` fenced block in *text* that parses as an object."""
    objects: list[dict[str, Any]] = []
    for chunk in text.split("```json")[1:]:
        body = chunk.split("```", 1)[0]
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            objects.append(parsed)
    return objects


def zed_server_argv() -> list[str]:
    """Return the argv Zed spawns, asserting the schema is the current one."""
    settings = json.loads(ZED_SETTINGS.read_text(encoding="utf-8"))
    entry = settings["context_servers"]["msmcp"]
    command = entry["command"]
    assert isinstance(command, str), (
        "`.zed/settings.json` must use Zed's current schema - a `command` "
        "string plus an `args` array (https://zed.dev/docs/ai/mcp). Found the "
        f"pre-2025-11-25 nested object form: {command!r}"
    )
    args = entry["args"]
    assert isinstance(args, list), f"`args` must be a list, got {args!r}"
    return [command, *args]


def test_zed_config_uses_flat_command_schema() -> None:
    """The launch line is exactly `uv run msmcp`, in the current Zed schema."""
    assert zed_server_argv() == EXPECTED_ARGV


def test_readme_zed_snippet_matches_settings_file() -> None:
    """README's published Zed block cannot drift from the shipped file."""
    documented = [
        obj
        for obj in _json_objects_in_fences(README.read_text(encoding="utf-8"))
        if "context_servers" in obj
    ]
    assert len(documented) == 1, (
        f"expected exactly one context_servers block in README.md, got {len(documented)}"
    )
    assert documented[0] == json.loads(ZED_SETTINGS.read_text(encoding="utf-8"))


@pytest.mark.skipif(shutil.which("uv") is None, reason="uv is not on PATH")
def test_configured_argv_serves_mcp_over_stdio() -> None:
    """The configured argv really spawns a working stdio MCP server."""
    proc = subprocess.Popen(
        zed_server_argv(),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert proc.stdin is not None and proc.stdout is not None
    assert proc.stderr is not None
    try:
        _handshake(proc)
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=30)


def _handshake(proc: subprocess.Popen[str]) -> None:
    """Drive ``initialize`` + ``tools/list`` and assert the server is live."""
    requests = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "zed-config-test", "version": "0.0.1"},
            },
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
    ]

    # stderr is drained concurrently: the server logs at INFO and a full pipe
    # buffer would deadlock the child mid-session (same reason as
    # tests/test_smoke_stdio.py).
    stdout_lines: list[str] = []
    stderr_lines: list[str] = []

    def _drain(stream: Any, sink: list[str]) -> None:
        for line in stream:
            sink.append(line)

    readers = [
        threading.Thread(target=_drain, args=(proc.stdout, stdout_lines), daemon=True),
        threading.Thread(target=_drain, args=(proc.stderr, stderr_lines), daemon=True),
    ]
    for reader in readers:
        reader.start()

    assert proc.stdin is not None
    for request in requests:
        proc.stdin.write(json.dumps(request) + "\n")
    proc.stdin.flush()

    deadline = time.monotonic() + 60.0
    responses: list[dict[str, Any]] = []
    while time.monotonic() < deadline:
        # Parsing fails loudly on any non-JSON stdout line, i.e. on stray logs.
        responses = [json.loads(line) for line in stdout_lines if line.strip()]
        if len(responses) >= 2:
            break
        time.sleep(0.05)
    else:
        pytest.fail(
            f"no server responses within 60s: stdout={stdout_lines!r} "
            f"stderr={stderr_lines[-5:]!r}"
        )

    by_id = {response.get("id"): response for response in responses}
    init = by_id.get(1)
    assert init is not None, f"no response to initialize: {responses!r}"
    assert "error" not in init, init
    assert init["result"]["serverInfo"]["name"] == "MSMCP-MassFlow-Adapter"

    listed = by_id.get(2)
    assert listed is not None, f"no response to tools/list: {responses!r}"
    assert "error" not in listed, listed
    assert listed["result"]["tools"], "tools/list returned no tools"

    assert proc.stdin is not None
    proc.stdin.close()
    proc.wait(timeout=30)
    for reader in readers:
        reader.join(timeout=5)
    assert proc.returncode == 0, f"server exited {proc.returncode}: {stderr_lines[-5:]}"
```

Run it and confirm it fails for the right reasons:

```bash
uv run pytest tests/test_zed_config.py -q
```

- **Expected: `2 failed, 1 passed`**.
  - `test_zed_config_uses_flat_command_schema` fails with `AssertionError: .zed/settings.json must use Zed's current schema … Found the pre-2025-11-25 nested object form: {'path': 'uv', 'args': ['run', 'msmcp']}`.
  - `test_configured_argv_serves_mcp_over_stdio` fails at the same assertion (`subprocess.Popen` would otherwise receive a dict as argv[0]).
  - `test_readme_zed_snippet_matches_settings_file` **passes** — README and the file are *consistently stale* right now. This test is a drift guard, not a detector of the original defect; it goes red in Task 3 if the file is fixed and README is not.
- If more than these 3 tests are collected, something else broke — stop and inspect.

Then check formatting/lint of the new file before going further:

```bash
uv run ruff format --check tests/test_zed_config.py
uv run ruff check tests/test_zed_config.py
```

- Expected: `1 file already formatted` and `All checks passed!`.
- If `ruff format` reports a change, run `uv run ruff format tests/test_zed_config.py` and re-check. Do not hand-fight the formatter.

### Task 3 — Apply the fix (TDD: GREEN)

**3a. Restore the fixed `.zed/settings.json`** by writing this exact content:

```json
{
  "context_servers": {
    "msmcp": {
      "command": "uv",
      "args": ["run", "msmcp"],
      "env": {}
    }
  }
}
```

(Equivalently: `git apply /tmp/zed-fix.patch`.)

**3b. Fix the README block at `README.md:196-209`.** Replace:

```json
{
  "context_servers": {
    "msmcp": {
      "command": {
        "path": "uv",
        "args": ["run", "msmcp"]
      }
    }
  }
}
```

with:

```json
{
  "context_servers": {
    "msmcp": {
      "command": "uv",
      "args": ["run", "msmcp"],
      "env": {}
    }
  }
}
```

Leave the surrounding prose (`MSMCP is a child-process MCP server. …`) intact. Optional but recommended, so nobody "fixes" it back: append one sentence after the block —

> `command`/`args` is the current Zed schema; the nested `command: {path, args}` object was migrated away by Zed's 2025-11-25 settings migration.

**3c. Re-run the tests:**

```bash
uv run pytest tests/test_zed_config.py -q
```

- **Expected: `3 passed`** (about 1–3 s; the third test spawns `uv run msmcp`).
- If the third test fails with a `pytest.fail(...)`, capture the printed `stderr` tail — that is the server's own diagnostic.

### Task 4 — Run the full gate in CI order

```bash
uv run ruff format --check .
make lint
make test
make eval
```

- `uv run ruff format --check .` → expected **`49 files already formatted`** (48 baseline + 1 new test file).
- `make lint` → expected `All checks passed!` / `Success: no issues found in 43 source files` (42 + 1) / `0 errors, 0 warnings, 0 notes`.
  - Note: `basedpyright` and `mypy` both cover `tests/` (see `pyproject.toml:78`, and its `exclude` list at lines 90-105 does **not** exclude `tests`), so the new file must type-check clean.
- `make test` → expected **`426 passed, 1 deselected`** (423 baseline + 3).
- `make eval` → must match the Task 0 baseline (same pass/fail), since no production code changed.

If `uv run ruff format --check .` complains about `README.md`-adjacent files, that is a pre-existing issue — re-check Task 0's baseline before "fixing" anything unrelated.

### Task 5 — Commit on a branch (never `main`)

```bash
cd /Users/ericjanusson/Programming/msmcp
git fetch origin
git log --oneline origin/main -3
git switch -c fix/zed-context-server-schema
git add .zed/settings.json README.md tests/test_zed_config.py
git status --short
git diff --cached --stat
git commit -m "Use Zed's current context-server schema in .zed/settings.json"
```

- Branch name follows the repo's existing convention (cf. `feat/compute-cosine-data-references`).
- **`git fetch` first:** Eric runs parallel agent/Copilot sessions against these repos and `main` may have moved.
- Commit message style matches this repo's history (imperative sentence case, no conventional-commit prefix — cf. `Fix repeated report delivery race`).
- Expected final `git status --short`: three staged/committed entries and **no** `.hermes/` entry (see Task 7).
- Inspect `git diff --cached --stat` before committing: `.zed/settings.json` (~5 lines), `README.md` (~3 lines), `tests/test_zed_config.py` (new, ~150 lines). Anything else staged means `git add` was too broad.

### Task 6 — Push and open a PR (Eric's protocol: branch + PR, verified by URL)

```bash
cd /Users/ericjanusson/Programming/msmcp
git -c credential.helper= -c credential.helper='!gh auth git-credential' push -u origin fix/zed-context-server-schema
gh pr create --base main --fill
gh pr view --json url,state,title
```

- Plain `git push` fails in a Hermes shell with `could not read Username`; the `gh` credential-helper form above is required.
- Expected `gh pr view`: JSON containing a real `"url"`, `"state": "OPEN"`, and the commit title.
- Report the URL. A self-reported "opened" is not proof — the `gh pr view` output is.
- CI must be green before handoff: `gh pr checks --watch`.

### Task 7 — Explicitly do NOT touch these

| Path | Why |
|---|---|
| `docs/mcp-server-configuration.md` | Lines 1-5 state it is an **unedited transcript** of the design sessions, "not maintained as documentation; some options discussed here were not taken". Lines 125/140/166/233/246/323/375 embed the legacy form **as history**. Rewriting it falsifies a record. It is also excluded from ruff via `pyproject.toml:64`. |
| `examples/*.html`, `ARCHITECTURE.md` | Contain no MCP host config — verified by grep; nothing to fix. |
| `~/.config/zed/settings.json` (global, outside the repo) | Contains a `"msmcp": {"enabled": false, …}` entry at lines 83-87. Editing it is outside this repo's scope and needs Eric's decision — see Open questions. |
| `.hermes/` | The plan directory this file lives in is **not** gitignored (`git check-ignore .hermes/plans/x.md` → not ignored). Keep it out of commits; if it should be ignored, that is a separate one-line `.gitignore` change — ask first. |

---

## Tests / validation

| Layer | Test | Detects |
|---|---|---|
| Schema shape | `test_zed_config_uses_flat_command_schema` | Legacy nested `command` object; wrong launcher; wrong args |
| Doc drift | `test_readme_zed_snippet_matches_settings_file` | README and the shipped file diverging (e.g. fixing only one of the two) |
| Wire | `test_configured_argv_serves_mcp_over_stdio` | The configured argv not spawning a working stdio MCP server (broken console script, logging on stdout, missing tool registration) |

TDD cycle per task, as run above: Task 2 writes tests and observes the failure (`2 failed, 1 passed`) → Task 3 applies the fix and observes `3 passed` → Task 4 runs the repo gate in CI's order → Task 5 commits → Task 6 opens the PR.

Full-suite expectation: **426 passed, 1 deselected** (baseline 423 + 3 new).

**Not verifiable by this test suite** (state this in the handoff rather than implying otherwise): that a real Zed instance spawns the server from this file. That requires the Zed GUI (Settings → AI → MCP Servers, indicator dot green / "Server is active"). It is a one-click manual check available to Eric.

---

## Risks, tradeoffs, and open questions

1. **The project-local file may be inert regardless of schema.** The user's global `~/.config/zed/settings.json` (lines 83-87) already holds `"msmcp": {"enabled": false, "remote": false, "settings": {}}`. Whether the project entry merges with that into an `enabled: false` server is **not verified** — it depends on Zed's per-key merge, which I did not test. **Ask Eric** to check Settings → AI → MCP Servers; if the indicator is not green, the fix to remove/adjust the global entry belongs in his macOS config, outside this repo, and needs his decision. Do not edit the global file from this task.
2. **Why fix the file at all if Zed migrates it?** Zed's migrator demonstrably rewrites this shape for *settings* it loads, and the installed 1.22.0 contains that migration. Depending on a migration for a repo-tracked file is fragile (it also silently rewrites user config), and whether project-local settings are migrated was not verified. Fixing the source of truth is the smaller risk. Tradeoff: editors older than the migration will not parse the new form — accepted, since Zed 1.22.0 is what is installed here.
3. **Test-suite cost.** The new spawn test adds a `uv run msmcp` child process (~1-3 s) to the fast suite. Accepted: it is the only test that exercises the exact line Zed runs. If Eric prefers the suite to stay sub-second, drop `test_configured_argv_serves_mcp_over_stdio` and keep the two pure-JSON tests — the schema test still catches the original defect deterministically.
4. **README prose is untested beyond the JSON block.** Someone could re-add a stale block elsewhere in README.md and the drift test would only complain if a second `context_servers` block appears (it asserts exactly one). That is intentional: it fails loudly rather than silently picking one.
5. **Open question — `make eval` baseline.** Task 0 records it; if it is red before any change, that is pre-existing and out of scope, but must be reported rather than worked around.
6. **Open question — `.hermes/` in the repo.** Untracked planning files will show in `git status` for every future session. Add `.hermes/` to `.gitignore` (one line, matching the existing `drafts/` entry at `.gitignore:233`)? Ask Eric.
7. **`massflow[chem]` (noted, not actioned).** The project's own design log lists it as a strict dependency, whereas `pyproject.toml:28-30` deliberately models RDKit as an optional `chem` extra with a fallback comment. Making it strict is a semantic change nobody asked for; leave it.

---

## Appendix — reproduction commands used for the evidence above

```bash
# Current schema per Zed docs
curl -sL https://zed.dev/docs/ai/mcp | grep -A12 '"context_servers"'

# The migration that flattens the legacy form (test fixtures included)
curl -sL https://raw.githubusercontent.com/zed-industries/zed/main/crates/migrator/src/migrator.rs \
  | grep -A40 test_flatten_context_server_command

# Installed Zed carries that migration
grep -c -a "2025-11-25" /Applications/Zed.app/Contents/MacOS/zed   # -> 2

# The only two config sites in the repo
grep -rn '"path": "uv"\|context_servers' --include=*.md --include=*.json . | grep -v '^./drafts/'
```

Post-fix spot check (expects the flat form and a live handshake):

```bash
uv run python -c "import json,pathlib; print(json.loads(pathlib.Path('.zed/settings.json').read_text()))"
uv run pytest tests/test_zed_config.py -v
```

---

## Execution log (2026-10-07, Talos) — landing step PENDING

Tasks 0-4 were executed. Outcome, with the real numbers:

| Step | Result |
|---|---|
| Baseline (Task 0) | ruff format `49 files already formatted`; lint clean (mypy 42 files, basedpyright 0 errors); `423 passed, 1 deselected`; `make eval` **PASSED** (109 cases, 1 skip, 34.4 s) |
| RED (Task 2) | `2 failed, 1 passed` exactly as predicted, with the assertion naming `{'path': 'uv', 'args': ['run', 'msmcp']}` |
| GREEN (Task 3) | `3 passed in 0.51 s`; restored `.zed/settings.json` byte-identical to the saved patch |
| Gate (Task 4) | ruff format `53 files`; lint clean (mypy 46 files, basedpyright 0/0/0); `452 passed, 1 deselected`; `make eval` PASSED (114 cases, 1 skip, 30.6 s) |

**The numbers exceed this plan's predictions because the working tree changed under it.** A
parallel agent session checked out `feat/msp-library-provider` in this directory mid-task
(HEAD moved `4a7ec82` → `ea34d78`; 3 commits, +3 source files, +1 test file, +26 tests, +5 eval
cases, all authored `janussone` and already pushed). Reconciliation: 423 baseline + 26 theirs +
3 mine = 452; ruff 49 + 3 theirs + 1 mine = 53; mypy 42 + 3 + 1 = 46. My edits did not disturb
any of their work (README diff base is their committed `ca0f547`).

**Correction to the plan's premise:** this tree was NOT on `main` by the time Tasks 5-6 ran. It
sits on `feat/msp-library-provider`, so `git switch -c fix/zed-context-server-schema` would
branch from that F4 work and any PR would carry the F4 commits. Tasks 5-6 (commit, push, PR)
were therefore **not executed** — that is an ownership decision for Eric, not an autonomous one.
Awaiting his choice: (a) branch off `origin/main` in a separate worktree and commit only the
Zed fix, (b) commit onto `feat/msp-library-provider`, or (c) leave uncommitted.

Current uncommitted state on `feat/msp-library-provider` @ `ea34d78`: `M .zed/settings.json`,
`M README.md`, `?? tests/test_zed_config.py`. Backups of all three live in `$TMPDIR`:
`zed-work-tracked.patch` (tracked files) and `test_zed_config.py.bak` (untracked test).

Risk while the decision is open: another session running `git add -A` in this tree would sweep
these changes into its own commit.

---

## Final disposition (verified 2026-10-09)

The Zed fix was committed separately on `fix/zed-context-server-schema` as
`afeb224` (`Use Zed's current context-server schema in .zed/settings.json`) and opened as
[PR #37](https://github.com/janusson/MSMCP/pull/37). The PR changes `.zed/settings.json`,
`README.md`, and adds `tests/test_zed_config.py`.

**Status at verification: PR #37 is open and unmerged.** Do not describe the fix as landed on
`main` until the PR is merged. This checkout is still on `feat/msp-library-provider` at `ea34d78`;
its `.zed/settings.json` and README still contain the legacy nested command form, and it does not
contain `tests/test_zed_config.py`. No cherry-pick or merge was performed into this checkout.

The PR's existence and open state were checked on GitHub on 2026-10-09. This note records status
at that time and may become stale if the PR is updated or merged later.
