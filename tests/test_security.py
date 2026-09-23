"""Tests for the local file-access boundary in ``msmcp.security``."""

from __future__ import annotations

from pathlib import Path

import pytest

from msmcp.security import (
    DEFAULT_MAX_FILE_SIZE_BYTES,
    DEFAULT_MAX_SPECTRA,
    FileSizeExceededError,
    PathEscapeError,
    SecurityPolicy,
    SpectrumCountExceededError,
    resolve_path,
    validate_file_size,
    validate_spectrum_count,
)


@pytest.fixture()
def sandbox(tmp_path: Path) -> tuple[Path, Path]:
    """Return ``(allowed_root, outside_dir)`` in two sibling temp dirs."""
    allowed = tmp_path / "allowed"
    outside = tmp_path / "outside"
    allowed.mkdir()
    outside.mkdir()
    return allowed, outside


# ---------------------------------------------------------------------------
# Path containment and symlink escape
# ---------------------------------------------------------------------------
class TestResolvePath:
    def test_path_within_root_is_returned_resolved(
        self, sandbox: tuple[Path, Path]
    ) -> None:
        allowed, _ = sandbox
        target = allowed / "data.mzML"
        target.write_text("peaks")
        policy = SecurityPolicy(allowed_root=allowed)

        assert resolve_path(target, policy) == target.resolve()

    def test_relative_path_resolves_against_cwd(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        policy = SecurityPolicy(allowed_root=tmp_path)
        target = tmp_path / "data.mzML"
        target.write_text("peaks")

        # A relative path under the working directory; force the cwd to tmp_path.
        monkeypatch.chdir(tmp_path)
        assert resolve_path("data.mzML", policy) == target.resolve()

    def test_path_outside_root_raises(self, sandbox: tuple[Path, Path]) -> None:
        allowed, outside = sandbox
        secret = outside / "secret.txt"
        secret.write_text("nope")
        policy = SecurityPolicy(allowed_root=allowed)

        with pytest.raises(PathEscapeError, match="outside the allowed root"):
            resolve_path(secret, policy)

    def test_symlink_escaping_root_raises(self, sandbox: tuple[Path, Path]) -> None:
        allowed, outside = sandbox
        secret = outside / "secret.mzML"
        secret.write_text("nope")
        link = allowed / "innocent.mzML"
        link.symlink_to(secret)

        policy = SecurityPolicy(allowed_root=allowed)
        with pytest.raises(PathEscapeError, match="Symlinked paths"):
            resolve_path(link, policy)

    def test_symlink_within_root_is_allowed(self, sandbox: tuple[Path, Path]) -> None:
        allowed, _ = sandbox
        real = allowed / "real.mzML"
        real.write_text("peaks")
        alias = allowed / "alias.mzML"
        alias.symlink_to(real.name)  # relative symlink that stays inside the root

        policy = SecurityPolicy(allowed_root=allowed)
        assert resolve_path(alias, policy) == real.resolve()


# ---------------------------------------------------------------------------
# File size and spectrum count limits
# ---------------------------------------------------------------------------
class TestLimits:
    def test_file_size_limit_rejects_oversized_file(self, tmp_path: Path) -> None:
        big = tmp_path / "big.mzML"
        big.write_bytes(b"x" * 10)
        policy = SecurityPolicy(allowed_root=tmp_path, max_file_size_bytes=5)

        with pytest.raises(FileSizeExceededError, match="exceeds"):
            validate_file_size(big, policy)

    def test_file_size_limit_accepts_small_file(self, tmp_path: Path) -> None:
        small = tmp_path / "small.mzML"
        small.write_bytes(b"x" * 10)
        policy = SecurityPolicy(allowed_root=tmp_path, max_file_size_bytes=10)

        validate_file_size(small, policy)  # does not raise

    def test_spectrum_count_limit(self) -> None:
        policy = SecurityPolicy(max_spectra=5)
        validate_spectrum_count(5, policy)  # boundary value is allowed

        with pytest.raises(SpectrumCountExceededError, match="spectrum count"):
            validate_spectrum_count(6, policy)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
class TestPolicy:
    def test_defaults(self, tmp_path: Path) -> None:
        policy = SecurityPolicy(allowed_root=tmp_path)
        assert policy.max_file_size_bytes == DEFAULT_MAX_FILE_SIZE_BYTES
        assert policy.max_spectra == DEFAULT_MAX_SPECTRA

    def test_from_env(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MSMCP_ALLOWED_ROOT", str(tmp_path))
        monkeypatch.setenv("MSMCP_MAX_FILE_SIZE_BYTES", "1024")
        monkeypatch.setenv("MSMCP_MAX_SPECTRA", "42")

        policy = SecurityPolicy.from_env()
        assert policy.allowed_root == tmp_path.resolve()
        assert policy.max_file_size_bytes == 1024
        assert policy.max_spectra == 42

    def test_from_env_overrides_take_precedence(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MSMCP_MAX_SPECTRA", "42")
        policy = SecurityPolicy.from_env(max_spectra=7, allowed_root=tmp_path)
        assert policy.max_spectra == 7

    @pytest.mark.parametrize("raw", ["abc", "-1", "0"])
    def test_from_env_rejects_invalid_integers(
        self, monkeypatch: pytest.MonkeyPatch, raw: str
    ) -> None:
        monkeypatch.setenv("MSMCP_MAX_FILE_SIZE_BYTES", raw)
        with pytest.raises(ValueError):
            SecurityPolicy.from_env()
