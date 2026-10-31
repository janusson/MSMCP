"""Filesystem access boundary for MSMCP tools.

Every tool that touches the local filesystem must pass its paths through this
module.  The boundary enforces three hard limits:

- **Allowed root** — a single directory subtree outside which no file may be
  read.  Paths are resolved (symlinks included) before the containment check,
  so a symlink that points outside the root is rejected rather than followed.
- **File size** — a maximum byte size per file, so a path inside the root can
  never cause the server to read an unbounded blob.
- **Spectrum count** — a maximum number of spectra a single request may ask to
  process, capping the per-request memory/CPU footprint.

The limits are configured through :class:`SecurityPolicy`.  Defaults can be
overridden by the environment variables ``MSMCP_ALLOWED_ROOT``,
``MSMCP_MAX_FILE_SIZE_BYTES``, and ``MSMCP_MAX_SPECTRA`` (see
:meth:`SecurityPolicy.from_env`).
"""

from __future__ import annotations

import io
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from msmcp.errors import InaccessiblePathError, MsmcpError

__all__ = [
    "DEFAULT_MAX_FILE_SIZE_BYTES",
    "DEFAULT_MAX_SPECTRA",
    "DEFAULT_POLICY",
    "FileSizeExceededError",
    "PathEscapeError",
    "SecurityError",
    "SecurityPolicy",
    "SpectrumCountExceededError",
    "bounded_stream",
    "resolve_path",
    "validate_file_size",
    "validate_readable_file",
    "validate_spectrum_count",
]

# ---------------------------------------------------------------------------
# Limits and configuration knobs
# ---------------------------------------------------------------------------
DEFAULT_MAX_FILE_SIZE_BYTES = 500 * 1024 * 1024  # 500 MiB
"""Default per-file size ceiling."""

DEFAULT_MAX_SPECTRA = 1_000
"""Default per-request spectrum ceiling."""

ENV_ALLOWED_ROOT = "MSMCP_ALLOWED_ROOT"
ENV_MAX_FILE_SIZE = "MSMCP_MAX_FILE_SIZE_BYTES"
ENV_MAX_SPECTRA = "MSMCP_MAX_SPECTRA"


# ---------------------------------------------------------------------------
# Errors — one distinct, actionable type per boundary violation
# ---------------------------------------------------------------------------
class SecurityError(MsmcpError):
    """Base class for filesystem-access boundary violations."""


class PathEscapeError(SecurityError):
    """Raised when a path resolves outside the configured allowed root.

    Symlinked paths are resolved before the containment check, so this error
    also fires when a symlink inside the root points outside it.
    """


class FileSizeExceededError(SecurityError):
    """Raised when a file exceeds the configured maximum size."""


class SpectrumCountExceededError(SecurityError):
    """Raised when a request exceeds the configured spectrum-count limit."""


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------
def _parse_positive_int(raw: str, var: str) -> int:
    """Parse a strictly positive integer from *raw*, naming *var* on error."""
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"{var} must be an integer, got {raw!r}") from None
    if value <= 0:
        raise ValueError(f"{var} must be a positive integer, got {raw!r}")
    return value


@dataclass(frozen=True, slots=True)
class SecurityPolicy:
    """Immutable configuration for the local file-access boundary.

    ``allowed_root`` is stored as an absolute, symlink-resolved :class:`Path`.
    Relative paths passed to :func:`resolve_path` are interpreted against the
    process working directory and must resolve inside this root.
    """

    allowed_root: Path = field(default_factory=Path.cwd)
    max_file_size_bytes: int = DEFAULT_MAX_FILE_SIZE_BYTES
    max_spectra: int = DEFAULT_MAX_SPECTRA

    def __post_init__(self) -> None:
        root = Path(self.allowed_root).expanduser().resolve(strict=False)
        object.__setattr__(self, "allowed_root", root)
        if self.max_file_size_bytes <= 0:
            raise ValueError("max_file_size_bytes must be a positive integer")
        if self.max_spectra <= 0:
            raise ValueError("max_spectra must be a positive integer")

    @classmethod
    def from_env(cls, **overrides: Any) -> SecurityPolicy:
        """Build a policy from the environment, with optional explicit overrides.

        Explicit ``overrides`` take precedence over environment variables,
        which in turn take precedence over the defaults.
        """
        kwargs: dict[str, Any] = dict(overrides)

        if "allowed_root" not in kwargs:
            raw_root = os.environ.get(ENV_ALLOWED_ROOT)
            if raw_root:
                kwargs["allowed_root"] = raw_root

        if "max_file_size_bytes" not in kwargs:
            raw_size = os.environ.get(ENV_MAX_FILE_SIZE)
            if raw_size:
                kwargs["max_file_size_bytes"] = _parse_positive_int(
                    raw_size, ENV_MAX_FILE_SIZE
                )

        if "max_spectra" not in kwargs:
            raw_spectra = os.environ.get(ENV_MAX_SPECTRA)
            if raw_spectra:
                kwargs["max_spectra"] = _parse_positive_int(
                    raw_spectra, ENV_MAX_SPECTRA
                )

        return cls(**kwargs)


# The process-wide default policy.  Environment variables are read once at
# import time, matching how the server is configured before it starts serving.
DEFAULT_POLICY = SecurityPolicy.from_env()
"""The default boundary used when a caller does not pass an explicit policy."""


# ---------------------------------------------------------------------------
# Enforcement
# ---------------------------------------------------------------------------
def resolve_path(
    path: str | os.PathLike[str],
    policy: SecurityPolicy = DEFAULT_POLICY,
) -> Path:
    """Resolve *path* and reject it if it escapes ``policy.allowed_root``.

    The path is made absolute (relative paths are interpreted against the
    working directory), expanded for ``~``, and resolved with symlinks
    followed.  If the final, canonical location is not inside the allowed root,
    :class:`PathEscapeError` is raised.
    """
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate

    resolved = candidate.resolve(strict=False)
    root = policy.allowed_root

    try:
        resolved.relative_to(root)
    except ValueError:
        raise PathEscapeError(
            f"Refusing to read {str(path)!r}: it resolves to {str(resolved)!r}, "
            f"outside the allowed root {str(root)!r}. Symlinked paths are "
            f"resolved and must stay within the configured root."
        ) from None

    return resolved


def validate_file_size(
    path: str | os.PathLike[str],
    policy: SecurityPolicy = DEFAULT_POLICY,
) -> None:
    """Raise :class:`FileSizeExceededError` if *path* exceeds the size limit.

    *path* should already exist and have passed :func:`resolve_path`; this
    function follows symlinks when stat-ing the file.
    """
    resolved = Path(path)
    size = resolved.stat().st_size

    if size > policy.max_file_size_bytes:
        raise FileSizeExceededError(
            f"File {str(resolved)!r} is {size} bytes, which exceeds the "
            f"configured maximum of {policy.max_file_size_bytes} bytes."
        )


def validate_spectrum_count(
    count: int,
    policy: SecurityPolicy = DEFAULT_POLICY,
) -> None:
    """Raise :class:`SpectrumCountExceededError` if *count* exceeds the limit."""
    if count > policy.max_spectra:
        raise SpectrumCountExceededError(
            f"Requested spectrum count {count} exceeds the configured maximum "
            f"of {policy.max_spectra}."
        )


def validate_readable_file(path: str | os.PathLike[str]) -> None:
    """Raise :class:`InaccessiblePathError` unless *path* is a readable file.

    The one access check every reader shares: the path must resolve to an
    existing regular file (not a directory, socket, FIFO or device) that can be
    opened for reading.  Each failure names the reason and raises a distinct
    typed error, so a caller can route on the error instead of catching a bare
    ``OSError``.  Keeping the check here rather than copy-pasted per reader is
    what stops the four ingestion paths from drifting apart.
    """
    resolved = Path(path)
    try:
        resolved.stat()
    except FileNotFoundError as exc:
        raise InaccessiblePathError(f"File not found: '{resolved}'") from exc
    except PermissionError as exc:
        raise InaccessiblePathError(
            f"Permission denied reading '{resolved}': {exc}"
        ) from exc
    except OSError as exc:
        raise InaccessiblePathError(f"Cannot access '{resolved}': {exc}") from exc

    if not resolved.is_file():
        raise InaccessiblePathError(f"Not a regular file: '{resolved}'")

    try:
        with resolved.open("rb"):
            pass
    except OSError as exc:
        raise InaccessiblePathError(f"File is not readable: '{resolved}'") from exc


# ---------------------------------------------------------------------------
# Bounded decompression
# ---------------------------------------------------------------------------
class _BoundedStream(io.BufferedIOBase):
    """A read-only binary stream that refuses to yield more than *max_bytes*.

    The readers wrap the *decompressed* side of a gzip input in this, because
    :func:`validate_file_size` can only measure the bytes on disk: a gzip file
    inside the allowed root and inside the size ceiling can still inflate to an
    arbitrary size.  Counting the bytes as they are consumed closes that gap
    without decompressing the whole file up front.
    """

    def __init__(self, stream: io.BufferedIOBase, max_bytes: int) -> None:
        self._stream = stream
        self._max_bytes = max_bytes
        self._read_bytes = 0

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return False

    def _raise_exceeded(self) -> None:
        raise FileSizeExceededError(
            f"Decompressed input exceeds the configured maximum of "
            f"{self._max_bytes} bytes; refusing to read further.  A "
            f"compressed file is bounded by its inflated size, not only by "
            f"the bytes on disk."
        )

    def _read_size(self, size: int | None) -> int:
        if self._read_bytes > self._max_bytes:
            self._raise_exceeded()
        maximum = self._max_bytes - self._read_bytes + 1
        if size is None or size < 0:
            return maximum
        return min(size, maximum)

    def _account(self, count: int) -> None:
        self._read_bytes += count
        if self._read_bytes > self._max_bytes:
            self._raise_exceeded()

    def read(self, size: int | None = -1) -> bytes:
        data = self._stream.read(self._read_size(size))
        self._account(len(data))
        return data

    def read1(self, size: int | None = -1) -> bytes:
        data = self._stream.read1(self._read_size(size))
        self._account(len(data))
        return data

    def readinto(self, buffer: Any) -> int:
        data = self._stream.read(self._read_size(len(buffer)))
        self._account(len(data))
        buffer[: len(data)] = data
        return len(data)

    def close(self) -> None:
        try:
            self._stream.close()
        finally:
            super().close()


def bounded_stream(stream: io.BufferedIOBase, max_bytes: int) -> io.BufferedIOBase:
    """Wrap *stream* so reading beyond *max_bytes* raises FileSizeExceededError.

    *max_bytes* at or below zero disables the bound and returns *stream*
    unchanged.  Callers pass the size ceiling in force for the path they are
    reading — for the process boundary that is
    ``DEFAULT_POLICY.max_file_size_bytes``, read at call time so a substituted
    policy takes effect.
    """
    if max_bytes <= 0:
        return stream
    return _BoundedStream(stream, max_bytes)
