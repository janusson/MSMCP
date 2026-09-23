"""First-class provenance for MSMCP's derived scientific objects.

Every significant object MSMCP derives from user data should be traceable
back to what it came from and how it was produced.  This module provides the
smallest coherent abstraction that supports that: immutable, structured
records rather than free-form strings, so a later reader can answer
mechanically:

* which operation produced this,
* against which source files (with format, reader backend and, when the file
  is small enough to hash cheaply, a content digest),
* with which parameters,
* from which parent :class:`~msmcp.state.pointers.DataReference`\\ s,
* under which software and model versions.

It is deliberately *not* a provenance framework: there is no graph database,
no lineage query language and no storage layer.  A :class:`Provenance` is a
value object that travels with the result it describes (and is embedded in the
:class:`~msmcp.state.pointers.DataReference`\\ s registered alongside it).

Timestamps are ISO 8601 strings in UTC, matching the MCP ``Task`` shape, and
all nested values are coerced to JSON-safe types by :func:`_jsonable` so a
record can be serialised into an MCP result without further work.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import platform
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final

import numpy as np

__all__ = [
    "DEFAULT_DIGEST_LIMIT_BYTES",
    "ModelInfo",
    "Provenance",
    "SoftwareInfo",
    "SourceRef",
    "file_digest",
    "provenance_for",
    "software_versions",
]


DEFAULT_DIGEST_LIMIT_BYTES: Final[int] = 64 * 1024 * 1024
"""Files larger than this are not hashed during provenance capture.

Hashing is I/O-bound; capping it keeps a provenance record cheap enough to
attach unconditionally.  ``digest`` is then reported as ``None`` rather than
silently omitted, so the absence is visible and explainable.
"""

_DIGEST_CHUNK_BYTES: Final[int] = 1024 * 1024

_TRACKED_PACKAGES: Final[tuple[str, ...]] = (
    "msmcp",
    "massflow",
    "numpy",
    "mcp",
    "rdkit",
    "torch",
)


def _jsonable(value: Any) -> Any:
    """Coerce *value* into JSON-safe data, recursing through containers."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(item) for item in value]
    # Unknown objects are not silently dropped: their repr keeps the record
    # honest about what was recorded, at the cost of not being round-trippable.
    return repr(value)


def _iso(timestamp: float) -> str:
    """Render a POSIX timestamp as an ISO 8601 UTC string."""
    return datetime.fromtimestamp(timestamp, tz=UTC).isoformat()


def _package_version(name: str) -> str | None:
    """Return the installed version of *name*, or ``None`` when absent."""
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None


def software_versions() -> dict[str, str]:
    """Return the versions of the packages relevant to reproducibility.

    Only distributions that are actually installed appear, so the mapping
    describes this installation rather than a hypothetical one.
    """
    versions = {"python": platform.python_version()}
    for name in _TRACKED_PACKAGES:
        version = _package_version(name)
        if version is not None:
            versions[name] = version
    return versions


def file_digest(
    path: str | Path,
    *,
    limit_bytes: int = DEFAULT_DIGEST_LIMIT_BYTES,
) -> str | None:
    """Return ``"sha256:<hex>"`` for *path*, or ``None`` if it is too large.

    Returns ``None`` rather than raising when the path cannot be read: a
    provenance record is metadata about a result, and must never be the reason
    a scientific operation fails.
    """
    resolved = Path(path)
    try:
        if resolved.stat().st_size > limit_bytes:
            return None
        digest = hashlib.sha256()
        with resolved.open("rb") as handle:
            while chunk := handle.read(_DIGEST_CHUNK_BYTES):
                digest.update(chunk)
    except OSError:
        return None
    return f"sha256:{digest.hexdigest()}"


# ---------------------------------------------------------------------------
# Structured sub-records
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class SoftwareInfo:
    """The software stack that performed an operation."""

    name: str = "msmcp"
    versions: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "versions", MappingProxyType(dict(self.versions)))

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe mapping."""
        return {"name": self.name, "versions": dict(self.versions)}


@dataclass(frozen=True, slots=True)
class ModelInfo:
    """Which model (and which weights) produced an embedding-based result.

    Recorded only for operations that actually ran a model, so a classical
    peak-matching result carries ``None`` instead of a misleading placeholder.
    """

    name: str
    backend: str
    embedding_dim: int | None = None
    checkpoint: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe mapping."""
        return {
            "name": self.name,
            "backend": self.backend,
            "embedding_dim": self.embedding_dim,
            "checkpoint": self.checkpoint,
        }


@dataclass(frozen=True, slots=True)
class SourceRef:
    """A description of one input that fed an operation.

    ``backend`` names the reader that actually parsed the file (for example
    ``"msmcp.mzml"`` or ``"massflow.imzML"``), which is what makes it possible
    to tell later whether two results were produced by the same parsing path.
    """

    path: str
    format: str | None = None
    backend: str | None = None
    size_bytes: int | None = None
    digest: str | None = None

    @classmethod
    def from_path(
        cls,
        path: str | Path,
        *,
        format: str | None = None,
        backend: str | None = None,
        digest_limit_bytes: int = DEFAULT_DIGEST_LIMIT_BYTES,
    ) -> SourceRef:
        """Capture *path*'s cheap metadata, hashing it when small enough."""
        resolved = Path(path)
        try:
            size = resolved.stat().st_size
        except OSError:
            size = None
        return cls(
            path=str(path),
            format=format,
            backend=backend,
            size_bytes=size,
            digest=file_digest(resolved, limit_bytes=digest_limit_bytes),
        )

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe mapping."""
        return {
            "path": self.path,
            "format": self.format,
            "backend": self.backend,
            "size_bytes": self.size_bytes,
            "digest": self.digest,
        }


# ---------------------------------------------------------------------------
# The record itself
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Provenance:
    """How one derived scientific object was produced.

    Instances are immutable: ``parameters`` is wrapped in a read-only mapping
    in :meth:`__post_init__`, so a record cannot be edited after the fact by
    the caller that created it.
    """

    operation: str
    """Name of the operation, e.g. ``"load_spectrum"`` or ``"search_library"``."""

    software: SoftwareInfo = field(default_factory=lambda: SoftwareInfo())
    parameters: Mapping[str, Any] = field(default_factory=dict)
    sources: tuple[SourceRef, ...] = ()
    parents: tuple[str, ...] = ()
    """Identifiers of parent :class:`DataReference`\\ s, as plain strings.

    Stored as strings rather than reference objects so this module stays free
    of any dependency on the reference store; the store depends on provenance,
    not the other way round.
    """

    model: ModelInfo | None = None
    created_at: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "parameters",
            MappingProxyType(dict(_jsonable(dict(self.parameters)))),
        )
        object.__setattr__(self, "sources", tuple(self.sources))
        object.__setattr__(self, "parents", tuple(str(p) for p in self.parents))

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe, MCP-serialisable mapping of the whole record."""
        return {
            "operation": self.operation,
            "software": self.software.to_dict(),
            "parameters": dict(self.parameters),
            "sources": [source.to_dict() for source in self.sources],
            "parents": list(self.parents),
            "model": self.model.to_dict() if self.model is not None else None,
            "created_at": _iso(self.created_at),
        }


def provenance_for(
    operation: str,
    *,
    parameters: Mapping[str, Any] | None = None,
    sources: tuple[SourceRef, ...] | list[SourceRef] = (),
    parents: tuple[str, ...] | list[str] = (),
    model: ModelInfo | None = None,
    software: SoftwareInfo | None = None,
) -> Provenance:
    """Build a :class:`Provenance` record for *operation*.

    ``software`` defaults to the versions detected in this installation, so
    callers normally pass only the operation, its parameters and its inputs.
    """
    return Provenance(
        operation=operation,
        software=software
        if software is not None
        else SoftwareInfo(versions=software_versions()),
        parameters=dict(parameters or {}),
        sources=tuple(sources),
        parents=tuple(parents),
        model=model,
    )
