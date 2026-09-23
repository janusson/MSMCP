"""Shared error hierarchy for MSMCP.

Tools raise these instead of returning a generic "analysis failed" string, so
an LLM host can tell *why* a request failed and route the next step
appropriately:

- :class:`InaccessiblePathError` — the file does not exist, is not a regular
  file, or cannot be read (permissions, I/O).
- :class:`MalformedFileError` — the file was opened but is not valid mzML
  (bad XML, truncated, corrupt binary arrays).
- :class:`MissingDependencyError` — an optional runtime dependency is required
  to process the file but is not installed.
- :class:`UnsupportedFormatError` — the format is recognised but not
  supported (vendor formats, or not-yet-implemented formats).

Filesystem-boundary violations (root escape, size, spectrum count) live in
:mod:`msmcp.security` and share the same :class:`MsmcpError` base here.
"""

from __future__ import annotations


class MsmcpError(Exception):
    """Base class for all expected MSMCP errors."""


class InaccessiblePathError(MsmcpError):
    """Raised when a path cannot be read: missing, not a file, or no access."""


class MalformedFileError(MsmcpError):
    """Raised when a file is opened but fails to parse as valid mzML."""


class MissingDependencyError(MsmcpError):
    """Raised when processing a file requires an optional package that is absent."""


class UnsupportedFormatError(MsmcpError):
    """Raised when a file's format is recognised but not supported."""


class SpectrumIndexError(MsmcpError):
    """Raised when a readable file has no spectrum at the requested index."""
