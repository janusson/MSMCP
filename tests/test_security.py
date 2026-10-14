"""Tests for the local file-access boundary in ``msmcp.security``."""

from __future__ import annotations

import gzip
import io
import os
from pathlib import Path

import pytest

from msmcp.errors import InaccessiblePathError
from msmcp.security import (
    DEFAULT_MAX_FILE_SIZE_BYTES,
    DEFAULT_MAX_SPECTRA,
    FileSizeExceededError,
    PathEscapeError,
    SecurityPolicy,
    SpectrumCountExceededError,
    bounded_stream,
    resolve_path,
    validate_file_size,
    validate_readable_file,
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

    def test_parent_traversal_is_refused(self, sandbox: tuple[Path, Path]) -> None:
        """``..`` that climbs out of the root must not resolve back inside it."""
        allowed, outside = sandbox
        secret = outside / "secret.txt"
        secret.write_text("nope")
        policy = SecurityPolicy(allowed_root=allowed)

        with pytest.raises(PathEscapeError, match="outside the allowed root"):
            resolve_path(allowed / ".." / "outside" / "secret.txt", policy)

    def test_intermediate_symlinked_directory_escape_is_refused(
        self, sandbox: tuple[Path, Path]
    ) -> None:
        """A symlinked *directory* inside the root that points outside it is refused.

        The escape need not be the final path component: resolving the whole
        path is what catches a symlink used as a directory.
        """
        allowed, outside = sandbox
        (outside / "secret.txt").write_text("nope")
        link_dir = allowed / "escape"
        link_dir.symlink_to(outside, target_is_directory=True)
        policy = SecurityPolicy(allowed_root=allowed)

        with pytest.raises(PathEscapeError, match="outside the allowed root"):
            resolve_path(link_dir / "secret.txt", policy)

    def test_allowed_root_itself_is_accepted(self, sandbox: tuple[Path, Path]) -> None:
        allowed, _ = sandbox
        policy = SecurityPolicy(allowed_root=allowed)
        assert resolve_path(allowed, policy) == allowed.resolve()


# ---------------------------------------------------------------------------
# The access check every reader shares
# ---------------------------------------------------------------------------
class TestValidateReadableFile:
    """``validate_readable_file`` is the one access check the readers share."""

    def test_a_readable_file_is_accepted(self, tmp_path: Path) -> None:
        path = tmp_path / "data.mzML"
        path.write_text("peaks")
        validate_readable_file(path)  # does not raise

    def test_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(InaccessiblePathError, match="not found"):
            validate_readable_file(tmp_path / "missing.mzML")

    def test_a_directory_is_not_a_regular_file(self, tmp_path: Path) -> None:
        directory = tmp_path / "data.mzML"
        directory.mkdir()
        with pytest.raises(InaccessiblePathError, match="Not a regular file"):
            validate_readable_file(directory)

    def test_a_symlink_to_a_file_is_accepted(self, tmp_path: Path) -> None:
        real = tmp_path / "real.mzML"
        real.write_text("peaks")
        link = tmp_path / "link.mzML"
        link.symlink_to(real.name)
        validate_readable_file(link)

    def test_an_unreadable_file_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "locked.mzML"
        path.write_text("peaks")
        path.chmod(0o000)
        try:
            if os.access(path, os.R_OK):  # e.g. running as root
                pytest.skip("file mode is bypassed by the current user")
            with pytest.raises(InaccessiblePathError, match="not readable"):
                validate_readable_file(path)
        finally:
            path.chmod(0o644)


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

    def test_file_size_limit_follows_a_symlink_to_an_oversized_target(
        self, sandbox: tuple[Path, Path]
    ) -> None:
        """The size check stat-follows a symlink, so it cannot be dodged by one."""
        allowed, _ = sandbox
        target = allowed / "big.mzML"
        target.write_bytes(b"x" * 100)
        alias = allowed / "alias.mzML"
        alias.symlink_to(target.name)
        policy = SecurityPolicy(allowed_root=allowed, max_file_size_bytes=10)

        with pytest.raises(FileSizeExceededError, match="exceeds"):
            validate_file_size(alias, policy)


class TestCompressedInputBudget:
    """The byte budget must bound what a compressed input inflates to.

    ``validate_file_size`` measures the bytes *on disk*, but every reader
    transparently inflates ``.gz`` inputs, so a small compressed file can expand
    far past ``max_file_size_bytes``.  The readers wrap the decompressed stream
    in :func:`msmcp.security.bounded_stream`, so the ceiling is applied to the
    bytes actually consumed rather than to the compressed file.
    """

    @staticmethod
    def _bomb(tmp_path: Path, name: str) -> Path:
        """Write a gzip file that is tiny on disk and inflates to ~1 MB."""
        path = tmp_path / name
        payload = b"100.0 1.0\n" * 100_000  # 1 MB inflated
        path.write_bytes(gzip.compress(payload, 9))  # ~2 KB on disk
        assert path.stat().st_size < 4096  # legal against the disk-size ceiling
        return path

    @staticmethod
    def _confine(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, max_bytes: int = 4096
    ) -> None:
        """Point the process boundary at *tmp_path* with a small byte budget."""
        from msmcp import security

        monkeypatch.setattr(
            security,
            "DEFAULT_POLICY",
            SecurityPolicy(allowed_root=tmp_path, max_file_size_bytes=max_bytes),
        )

    def test_a_non_positive_limit_disables_the_bound(self) -> None:
        stream = io.BytesIO(b"abc")
        assert bounded_stream(stream, 0) is stream

    def test_mgf_gzip_bomb_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from msmcp.mgf import iter_spectra

        self._confine(tmp_path, monkeypatch)
        with pytest.raises(FileSizeExceededError, match="Decompressed"):
            list(iter_spectra(self._bomb(tmp_path, "bomb.mgf.gz")))

    def test_mzml_gzip_bomb_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from msmcp.mzml import iter_spectra

        self._confine(tmp_path, monkeypatch)
        with pytest.raises(FileSizeExceededError, match="Decompressed"):
            list(iter_spectra(self._bomb(tmp_path, "bomb.mzML.gz")))

    def test_msp_library_gzip_bomb_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from msmcp import security
        from msmcp.library import get_library_provider

        self._confine(tmp_path, monkeypatch)
        bomb = self._bomb(tmp_path, "bomb.msp.gz")
        provider = get_library_provider(str(bomb), security.DEFAULT_POLICY)
        with pytest.raises(FileSizeExceededError, match="Decompressed"):
            list(provider.iter_spectra())

    def test_a_legitimate_compressed_input_still_reads(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The guard must not reject a normal gzip file under the default budget."""
        from msmcp import security
        from msmcp.mgf import iter_spectra

        monkeypatch.setattr(
            security, "DEFAULT_POLICY", SecurityPolicy(allowed_root=tmp_path)
        )
        path = tmp_path / "ok.mgf.gz"
        path.write_bytes(
            gzip.compress(b"BEGIN IONS\nPEPMASS=100.0\n120.0 1.0\nEND IONS\n")
        )
        assert len(list(iter_spectra(path))) == 1


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
