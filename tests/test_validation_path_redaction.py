"""The chromatogram verification report must not publish machine-specific paths.

``validation/chromatograms/verify_public.py`` records where it ran, so the numbers
it produces can be traced to a worktree, an environment and an input file. Those
records are published, so they must name the *relative* location without naming a
home directory, a checkout root or a scratch directory.

These tests use synthetic path inputs. They need no dataset, no MCP client and no
optional dependency: the normalizer is pure, and it is the whole of the fix.
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from typing import Any

import pytest

# Synthetic, deliberately fictional locations: no real user, no real scratch dir.
CHECKOUT = Path("/Users/someone/Programming/msmcp")
DATASET = Path("/private/tmp/masspilot-MSV000089046")


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


@pytest.fixture(scope="module")
def roots(verifier: Any) -> tuple[tuple[Path, str | None], ...]:
    """The root set the verifier passes for a real run.

    The checkout maps to ``None``: paths inside it are recorded relative, with no
    placeholder, because the relative path alone identifies them.
    """
    return (
        (CHECKOUT / ".venv", verifier.VENV_ROOT),
        (CHECKOUT, None),
        (DATASET, verifier.DATASET_DIR),
    )


def test_repository_paths_are_written_repository_relative(
    verifier: Any, roots: tuple[tuple[Path, str], ...]
) -> None:
    """A file inside the checkout needs no placeholder at all."""
    source = CHECKOUT / "src" / "msmcp" / "__init__.py"
    assert verifier.normalize_path(source, *roots) == "src/msmcp/__init__.py"
    manifest = CHECKOUT / "validation" / "chromatograms" / "MANIFEST.json"
    assert (
        verifier.normalize_path(manifest, *roots)
        == "validation/chromatograms/MANIFEST.json"
    )


def test_repository_root_and_its_venv_are_placeholders(
    verifier: Any, roots: tuple[tuple[Path, str], ...]
) -> None:
    assert verifier.normalize_path(CHECKOUT, *roots) == "<REPO_ROOT>"
    assert verifier.normalize_path(CHECKOUT / ".venv", *roots) == "<VENV_ROOT>"
    # The nested root wins over its parent, so an interpreter path stays legible.
    assert (
        verifier.normalize_path(CHECKOUT / ".venv" / "bin" / "python3.13", *roots)
        == "<VENV_ROOT>/bin/python3.13"
    )


def test_a_home_directory_is_never_emitted(
    verifier: Any, roots: tuple[tuple[Path, str], ...]
) -> None:
    """Every recordable location, normalized, must be free of this machine."""
    for value in [
        CHECKOUT / "src" / "msmcp" / "__init__.py",
        CHECKOUT / ".venv",
        CHECKOUT,
        DATASET / "Orbi_P_E2_CSSM10uMA.mzML",
        Path("/home/runner/work/MSMCP/MSMCP/x.mzML"),
        Path("/Users/someone/elsewhere/notes.md"),
        Path("/var/folders/xy/scratch.mzML"),
    ]:
        rendered = verifier.normalize_path(value, *roots)
        for leak in (
            "/Users",
            "/home/runner",
            "/private/tmp",
            "/var/folders",
            "someone",
        ):
            assert leak not in rendered, (value, rendered)


def test_dataset_paths_keep_the_file_name_and_the_dataset_identity(
    verifier: Any, roots: tuple[tuple[Path, str], ...]
) -> None:
    rendered = verifier.normalize_path(DATASET / "Orbi_P_E2_CSSM10uMA.mzML", *roots)
    assert rendered == "<TMP_DATASET_DIR>/Orbi_P_E2_CSSM10uMA.mzML"
    # The accession is carried by the report's own dataset_accession field, not by
    # the path, so dropping it here loses no provenance.
    assert "MSV000089046" not in rendered


def test_an_unrelated_absolute_path_keeps_only_its_final_component(
    verifier: Any, roots: tuple[tuple[Path, str], ...]
) -> None:
    assert (
        verifier.normalize_path("/home/runner/work/MSMCP/MSMCP/a.mzML", *roots)
        == "<LOCAL_PATH_REDACTED>/a.mzML"
    )


def test_a_sibling_directory_sharing_a_prefix_is_not_inside_the_repo(
    verifier: Any, roots: tuple[tuple[Path, str], ...]
) -> None:
    """``msmcp-other`` is not under ``msmcp``: a root is a path, not a prefix."""
    sibling = Path("/Users/someone/Programming/msmcp-other/src/msmcp/__init__.py")
    assert (
        verifier.normalize_path(sibling, *roots) == "<LOCAL_PATH_REDACTED>/__init__.py"
    )


def test_a_relative_path_is_already_portable(
    verifier: Any, roots: tuple[tuple[Path, str], ...]
) -> None:
    assert verifier.normalize_path("src/msmcp/__init__.py", *roots) == (
        "src/msmcp/__init__.py"
    )


@pytest.mark.skipif(os.name == "nt", reason="POSIX spelling under test")
def test_a_foreign_windows_path_does_not_leak_on_posix(
    verifier: Any, roots: tuple[tuple[Path, str | None], ...]
) -> None:
    """A drive-letter path cannot come from this platform, but an echoed input
    path arrives from a payload; absolute under either platform's rules is
    redacted, never published."""
    rendered = verifier.normalize_path(r"C:\Users\alice\data\a.mzML", *roots)
    assert rendered == "<LOCAL_PATH_REDACTED>/a.mzML"
    assert "alice" not in rendered


@pytest.mark.skipif(os.name != "nt", reason="Windows path semantics under test")
def test_windows_roots_normalize_like_unix_ones(verifier: Any) -> None:
    """On Windows the roots and the values agree, and output stays posix-joined."""
    checkout = Path("C:/Users/someone/Programming/msmcp")
    windows_roots = (
        (checkout / ".venv", verifier.VENV_ROOT),
        (checkout, verifier.REPO_ROOT),
    )
    rendered = verifier.normalize_path(
        checkout / "src" / "msmcp" / "__init__.py", *windows_roots
    )
    assert rendered == "src/msmcp/__init__.py"
    assert verifier.normalize_path(checkout / ".venv", *windows_roots) == "<VENV_ROOT>"
    assert "C:" not in rendered


def test_normalizing_does_not_change_what_was_measured(
    verifier: Any, roots: tuple[tuple[Path, str], ...]
) -> None:
    """Redaction rewrites the key that names an input, never its digest."""
    recorded = {
        DATASET / "Orbi_P_E2_CSSM10uMA.mzML": "sha256:91a112752847048b",
        DATASET / "Orbi_P_E2_EXBLANKA.mzML": "sha256:93eddd49a943beed",
    }
    normalized = {
        verifier.normalize_path(path, *roots): digest
        for path, digest in recorded.items()
    }
    assert sorted(normalized.values()) == sorted(recorded.values())
    assert set(normalized) == {
        "<TMP_DATASET_DIR>/Orbi_P_E2_CSSM10uMA.mzML",
        "<TMP_DATASET_DIR>/Orbi_P_E2_EXBLANKA.mzML",
    }


def test_serialization_is_repeatable_and_root_order_does_not_matter(
    verifier: Any, roots: tuple[tuple[Path, str], ...]
) -> None:
    value = CHECKOUT / ".venv" / "bin" / "python3.13"
    first = verifier.normalize_path(value, *roots)
    assert verifier.normalize_path(value, *roots) == first
    assert verifier.normalize_path(value, *reversed(roots)) == first
    dataset = verifier.normalize_path(DATASET / "a.mzML", *roots)
    assert verifier.normalize_path(DATASET / "a.mzML", *reversed(roots)) == dataset


def test_the_placeholders_are_the_documented_ones(verifier: Any) -> None:
    assert verifier.REPO_ROOT == "<REPO_ROOT>"
    assert verifier.VENV_ROOT == "<VENV_ROOT>"
    assert verifier.DATASET_DIR == "<TMP_DATASET_DIR>"
    assert verifier.LOCAL_PATH_REDACTED == "<LOCAL_PATH_REDACTED>"
