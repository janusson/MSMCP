"""The chromatogram verification report must not publish machine-specific paths.

``validation/chromatograms/verify_public.py`` records where it ran, so that the
numbers it produces can be traced back to a worktree, an environment and an input
file. Those records are published, so they must name the *relative* location
without naming a home directory, a checkout path or a temporary directory.

This tests the redaction the verifier applies to its own output. It needs no
dataset, no MCP client and no optional dependency: it exercises the pure helper
and the shape of the record it produces.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pytest


@pytest.fixture(scope="module")
def verifier() -> Any:
    """Load ``validation/chromatograms/verify_public.py`` by path."""
    path = (
        Path(__file__).resolve().parents[1]
        / "validation"
        / "chromatograms"
        / "verify_public.py"
    )
    spec = importlib.util.spec_from_file_location("verify_public", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_prefix_is_replaced_and_remainder_kept(verifier: Any) -> None:
    root = Path("/Users/someone/Programming/msmcp")
    value = "/Users/someone/Programming/msmcp/.worktrees/w/src/msmcp/__init__.py"
    assert (
        verifier.redact(value, root, verifier.REPO_ROOT)
        == "<REPO_ROOT>/.worktrees/w/src/msmcp/__init__.py"
    )


def test_the_root_itself_becomes_the_bare_placeholder(verifier: Any) -> None:
    root = Path("/Users/someone/Programming/msmcp")
    assert verifier.redact(str(root), root, verifier.REPO_ROOT) == "<REPO_ROOT>"
    # A trailing separator is not a different directory.
    assert verifier.redact(str(root) + "/", root, verifier.REPO_ROOT) == "<REPO_ROOT>/"


def test_a_sibling_directory_sharing_the_prefix_is_not_rewritten(verifier: Any) -> None:
    """``msmcp-other`` is not under ``msmcp``; a prefix is a path, not a string."""
    root = Path("/Users/someone/Programming/msmcp")
    sibling = "/Users/someone/Programming/msmcp-other/src/msmcp/__init__.py"
    assert verifier.redact(sibling, root, verifier.REPO_ROOT) == sibling


def test_an_unexpected_path_is_returned_unchanged(verifier: Any) -> None:
    """The verifier never invents a location it did not record itself."""
    root = Path("/tmp/dataset")
    assert verifier.redact("/somewhere/else/file.mzML", root, "<X>") == (
        "/somewhere/else/file.mzML"
    )


def test_the_recorded_placeholders_are_the_documented_ones(verifier: Any) -> None:
    assert verifier.REPO_ROOT == "<REPO_ROOT>"
    assert verifier.DATASET_DIR == "<TMP_DATASET_DIR>"
