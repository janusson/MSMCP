"""Tracked text must not publish a real machine path.

Records, transcripts and generated reports in this repository are public, and a
home directory or an OS scratch directory in one of them names the machine and
the person who produced it. Redaction alone does not stop the next one, so this
test scans every file ``git ls-files`` reports: what is published, not what is
ignored or local scratch.

The patterns are written so that this file never matches itself: each one has a
regex construct between its path components, and the detector's own checks
assemble their sample paths at runtime.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

_NAME = r"[A-Za-z0-9._-]+"
_WINDOWS_NAME = r"[A-Za-z0-9._ -]+"
MACHINE_PATH = re.compile(
    rf"/(?:Users|home)/(?P<posix_user>{_NAME})/"
    r"|/private/(?:tmp|var/folders)/"
    # Raw or JSON-escaped backslashes.
    rf"|\b[A-Za-z]:\\{{1,2}}Users\\{{1,2}}(?P<windows_user>{_WINDOWS_NAME})\\"
)

# User-name segments that are documentation placeholders, not people.
PLACEHOLDER_USERS = frozenset({"someone", "user", "username", "example"})

# Files allowed to contain machine-shaped paths, each with the reason. Keep this
# short: redact a record rather than adding it here.
ALLOWLIST: dict[str, str] = {
    "tests/test_search.py": (
        "synthetic CI and checkout paths proving the placeholder library is "
        "seeded from the file name only"
    ),
    "tests/test_validation_path_redaction.py": (
        "synthetic checkout, dataset and foreign paths the chromatogram "
        "verifier must redact"
    ),
}


def machine_paths(text: str) -> list[str]:
    """Return every real machine path in *text*; placeholders are not reported."""
    found: list[str] = []
    for match in MACHINE_PATH.finditer(text):
        user = match.group("posix_user") or match.group("windows_user")
        if user is not None and user.lower() in PLACEHOLDER_USERS:
            continue
        found.append(match.group(0))
    return found


def _tracked_files() -> list[str]:
    if not (REPO_ROOT / ".git").exists():
        pytest.skip("not a git checkout (e.g. an unpacked sdist)")
    listing = subprocess.run(
        ["git", "ls-files", "-z"], cwd=REPO_ROOT, capture_output=True, check=True
    ).stdout
    return sorted(name for name in listing.decode().split("\0") if name)


def test_no_tracked_file_contains_a_machine_path() -> None:
    offenders: list[str] = []
    for name in _tracked_files():
        path = REPO_ROOT / name
        if name in ALLOWLIST or path.is_symlink() or not path.is_file():
            continue
        data = path.read_bytes()
        if b"\0" in data[:8192]:  # binary fixture, e.g. a SQLite library
            continue
        text = data.decode("utf-8", errors="replace")
        for number, line in enumerate(text.splitlines(), start=1):
            offenders.extend(f"{name}:{number}: {hit}" for hit in machine_paths(line))
    assert not offenders, (
        "Tracked files publish machine-specific paths; replace them with a "
        "placeholder such as <REPO_ROOT>:\n" + "\n".join(offenders)
    )


def test_allowlist_names_tracked_files_only() -> None:
    stale = sorted(set(ALLOWLIST) - set(_tracked_files()))
    assert not stale, f"ALLOWLIST names files that are not tracked: {stale}"


@pytest.mark.parametrize(
    ("separator", "parts"),
    [
        ("/", ("", "Users", "alice", "notes.md")),
        ("/", ("", "home", "alice", "notes.md")),
        ("/", ("", "private", "tmp", "scratch", "a.mzML")),
        ("/", ("", "private", "var", "folders", "sz", "T", "a.mzML")),
        ("\\", ("C:", "Users", "alice", "a.mzML")),
        ("\\\\", ("C:", "Users", "Alice Smith", "a.mzML")),
    ],
)
def test_detector_flags_each_machine_path_form(
    separator: str, parts: tuple[str, ...]
) -> None:
    assert machine_paths(f"see {separator.join(parts)} for details")


@pytest.mark.parametrize(
    "text",
    [
        "/".join(("", "Users", "someone", "Programming", "msmcp")),
        "\\".join(("C:", "Users", "username", "msmcp")),
        "<REPO_ROOT>/src/msmcp/__init__.py",
        "/tmp/msmcp-review/notebooks",
    ],
)
def test_detector_ignores_placeholders_and_portable_paths(text: str) -> None:
    assert machine_paths(text) == []
