"""Server-side references to scientific data: the :class:`DataReference` model.

A :class:`DataReference` is a compact, opaque handle to scientific data that
lives on the server.  It exists so that a multi-step workflow can pass a large
dataset between tools *without* serialising it through MCP/JSON: the agent
carries a short identifier string, the server keeps the payload, and the next
tool dereferences the identifier in-process.

What this module does **not** claim
-----------------------------------
It does not claim "zero copy".  Registration takes one defensive copy of the
payload so the stored data can never be mutated behind the registry's back by
the caller that supplied it; that is a deliberate trade of one copy for
scientific correctness.  The property that matters for the agent is the one
that is actually guaranteed: **the payload is never serialised through
MCP/JSON, and is therefore never truncated by a context window.**

Storage representation
----------------------
Payloads are plain NumPy arrays (and the :class:`~msmcp.mzml.Spectrum`
dataclass, which is a pair of arrays plus scalars).  NumPy is already a hard
dependency; PyArrow is not installed, so no Arrow dependency is introduced.
Array buffers are made read-only at registration, i.e. stored scientific
payloads are effectively immutable.

Identifiers
-----------
An identifier has the form ``ptr:<kind>:<opaque-id>``, where ``<opaque-id>``
is :func:`secrets.token_hex` output.  Python object ids and raw memory
addresses are never used, so an identifier is safe to log and to hand to a
model.  An identifier is stable for the lifetime of its reference and is never
reused after release.
"""

from __future__ import annotations

import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any, Final

import numpy as np

from msmcp.errors import MsmcpError
from msmcp.mzml import Spectrum
from msmcp.provenance import Provenance

__all__ = [
    "DEFAULT_MAX_REFERENCES",
    "DEFAULT_MAX_TOTAL_BYTES",
    "DEFAULT_NAMESPACE",
    "IDENTIFIER_PREFIX",
    "DataKind",
    "DataReference",
    "ExpiredReferenceError",
    "PointerStore",
    "ReferenceError",
    "ReferenceKindError",
    "ReferencePayloadError",
    "StoreLimitError",
    "StoreStats",
    "UnknownReferenceError",
    "parse_identifier",
]

IDENTIFIER_PREFIX: Final[str] = "ptr"
"""Prefix identifying a string as an MSMCP data reference."""

DEFAULT_NAMESPACE: Final[str] = "default"
"""Namespace used when a caller does not request isolation."""

DEFAULT_MAX_REFERENCES: Final[int] = 128
"""Default ceiling on the number of live references in one store."""

DEFAULT_MAX_TOTAL_BYTES: Final[int] = 512 * 1024 * 1024
"""Default ceiling on the summed payload size of a store."""

_ID_BYTES: Final[int] = 12
"""Entropy (bytes) in an opaque identifier; 24 hex characters."""


# ---------------------------------------------------------------------------
# Errors — one distinct type per distinguishable failure
# ---------------------------------------------------------------------------
class ReferenceError(MsmcpError):
    """Base class for data-reference resolution failures."""


class UnknownReferenceError(ReferenceError):
    """Raised when an identifier is malformed or names no live reference."""


class ExpiredReferenceError(ReferenceError):
    """Raised when a reference existed but has passed its expiry."""


class ReferenceKindError(ReferenceError):
    """Raised when a reference exists but holds a different kind of data."""


class ReferencePayloadError(ReferenceError):
    """Raised when a payload cannot be stored as the requested kind."""


class StoreLimitError(ReferenceError):
    """Raised when storing a payload would exceed a configured store limit."""


# ---------------------------------------------------------------------------
# Kinds
# ---------------------------------------------------------------------------
class DataKind(StrEnum):
    """The scientific data types the store understands."""

    SPECTRUM = "spectrum"
    PEAK_LIST = "peak-list"
    EMBEDDING = "embedding"


def parse_identifier(identifier: str) -> tuple[DataKind, str]:
    """Split ``ptr:<kind>:<uid>`` into its parts.

    Raises
    ------
    UnknownReferenceError
        If *identifier* is not a well-formed MSMCP reference string.
    """
    parts = str(identifier).split(":")
    if len(parts) != 3 or parts[0] != IDENTIFIER_PREFIX or not parts[2]:
        raise UnknownReferenceError(
            f"{identifier!r} is not a valid data reference.  Expected the form "
            f"'{IDENTIFIER_PREFIX}:<kind>:<id>' as returned by the tool that "
            f"created it."
        )
    try:
        kind = DataKind(parts[1])
    except ValueError:
        known = ", ".join(k.value for k in DataKind)
        raise UnknownReferenceError(
            f"Unknown data kind {parts[1]!r} in reference {identifier!r}.  "
            f"Known kinds: {known}."
        ) from None
    return kind, parts[2]


# ---------------------------------------------------------------------------
# The reference
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class DataReference:
    """An opaque, server-side handle to one scientific payload.

    Carries only metadata: enough to describe and debug the payload (its kind,
    shape, size, provenance and lifetime) without embedding the data itself.
    There is deliberately no generation counter: identifiers are never reused,
    so a stale identifier can only fail to resolve - it can never resolve to a
    different payload.
    """

    kind: DataKind
    uid: str
    namespace: str = DEFAULT_NAMESPACE
    shape: tuple[int, ...] | None = None
    n_bytes: int | None = None
    created_at: float = 0.0
    expires_at: float | None = None
    provenance: Provenance | None = None

    @property
    def identifier(self) -> str:
        """The stable, opaque identifier string handed to the caller."""
        return f"{IDENTIFIER_PREFIX}:{self.kind.value}:{self.uid}"

    def to_dict(self) -> dict[str, Any]:
        """Return a compact, JSON-safe description for a tool result.

        Never includes the payload, which is the entire point of the type.
        """
        return {
            "reference": self.identifier,
            "kind": self.kind.value,
            "shape": list(self.shape) if self.shape is not None else None,
            "n_bytes": self.n_bytes,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "namespace": self.namespace,
            "provenance": (
                self.provenance.to_dict() if self.provenance is not None else None
            ),
        }


@dataclass(frozen=True, slots=True)
class StoreStats:
    """A snapshot of store occupancy, for diagnostics and tests."""

    count: int
    total_bytes: int
    max_references: int
    max_total_bytes: int
    namespaces: tuple[str, ...]


@dataclass(slots=True)
class _Entry:
    reference: DataReference
    payload: Any


# ---------------------------------------------------------------------------
# Payload normalisation
# ---------------------------------------------------------------------------
def _readonly_copy(value: Any, dtype: Any = np.float64) -> np.ndarray:
    """Return a C-contiguous, read-only copy of *value* in *dtype*."""
    copy = np.array(value, dtype=dtype, copy=True)
    copy.setflags(write=False)
    return copy


def _as_1d_array(value: Any, label: str) -> np.ndarray:
    """Coerce *value* to a read-only 1-D float64 array or explain why not."""
    try:
        array = np.asarray(value, dtype=np.float64)
    except (TypeError, ValueError) as exc:
        raise ReferencePayloadError(
            f"{label} must be numeric array data; got {type(value).__name__}."
        ) from exc
    if array.ndim != 1:
        raise ReferencePayloadError(
            f"{label} must be a 1-D array; got shape {array.shape}."
        )
    return _readonly_copy(array)


def _as_peak_list(value: Any) -> np.ndarray:
    """Coerce *value* to a read-only (N, 2) float64 array or explain why not."""
    try:
        array = np.asarray(value, dtype=np.float64)
    except (TypeError, ValueError) as exc:
        raise ReferencePayloadError(
            "a peak list must be numeric array data of shape (N, 2); got "
            f"{type(value).__name__}."
        ) from exc
    if array.ndim != 2 or array.shape[1] != 2:
        raise ReferencePayloadError(
            f"a peak list must have shape (N, 2); got {array.shape}."
        )
    if array.shape[0] == 0:
        raise ReferencePayloadError("a peak list must contain at least one peak.")
    return _readonly_copy(array)


def _as_spectrum(value: Any) -> Spectrum:
    """Return *value* as a :class:`Spectrum` with read-only arrays."""
    if not isinstance(value, Spectrum):
        raise ReferencePayloadError(
            "kind 'spectrum' requires an msmcp.mzml.Spectrum; got "
            f"{type(value).__name__}."
        )
    if value.mz.shape != value.intensity.shape:
        raise ReferencePayloadError(
            "a spectrum's m/z and intensity arrays must be the same length; got "
            f"{value.mz.shape} and {value.intensity.shape}."
        )
    return replace(
        value,
        mz=_as_1d_array(value.mz, "spectrum m/z array"),
        intensity=_as_1d_array(value.intensity, "spectrum intensity array"),
    )


def _normalise_payload(
    payload: Any, kind: DataKind
) -> tuple[Any, tuple[int, ...], int]:
    """Validate and freeze *payload*, returning (payload, shape, n_bytes)."""
    if kind is DataKind.SPECTRUM:
        spectrum = _as_spectrum(payload)
        n_bytes = int(spectrum.mz.nbytes + spectrum.intensity.nbytes)
        return spectrum, spectrum.mz.shape, n_bytes

    if kind is DataKind.PEAK_LIST:
        peaks = _as_peak_list(payload)
        return peaks, peaks.shape, int(peaks.nbytes)

    if kind is DataKind.EMBEDDING:
        vector = _as_1d_array(payload, "an embedding")
        return vector, vector.shape, int(vector.nbytes)

    raise ReferencePayloadError(f"Unsupported data kind {kind!r}.")


# ---------------------------------------------------------------------------
# The store
# ---------------------------------------------------------------------------
class PointerStore:
    """A bounded, thread-safe registry of server-side scientific payloads.

    The store owns the payloads it is handed.  Callers reach them only through
    :meth:`lookup` (or a convenience wrapper in :mod:`msmcp.state.store`),
    which is what makes it safe to share one store across the MCP event loop,
    worker threads running CPU-bound scans, and the threads that own the stdio
    transport.

    Lifecycle and limits
    --------------------
    * Every registration may carry a TTL; expired entries are removed lazily on
      the next access rather than by a background sweeper, so the store needs no
      scheduler and no thread of its own.
    * ``max_references`` and ``max_total_bytes`` bound memory.  Exceeding either
      raises :class:`StoreLimitError` *without* evicting live entries: silently
      discarding a payload the caller still holds an identifier for would turn a
      resource error into a wrong scientific answer.
    * Namespaces isolate independent workflows (for example one MCP session from
      another) so a reference cannot be dereferenced across them.
    """

    def __init__(
        self,
        *,
        max_references: int = DEFAULT_MAX_REFERENCES,
        max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES,
        default_ttl_seconds: float | None = None,
        clock: Callable[[], float] = time.monotonic,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        if max_references <= 0:
            raise ValueError("max_references must be positive")
        if max_total_bytes <= 0:
            raise ValueError("max_total_bytes must be positive")
        if default_ttl_seconds is not None and default_ttl_seconds <= 0:
            raise ValueError("default_ttl_seconds must be positive or None")
        self._max_references = max_references
        self._max_total_bytes = max_total_bytes
        self._default_ttl_seconds = default_ttl_seconds
        self._clock = clock
        self._id_factory = id_factory or (lambda: secrets.token_hex(_ID_BYTES))
        self._lock = threading.RLock()
        self._entries: dict[str, _Entry] = {}
        self._total_bytes = 0

    # -- introspection ---------------------------------------------------
    @property
    def max_references(self) -> int:
        """Configured ceiling on the number of live references."""
        return self._max_references

    @property
    def max_total_bytes(self) -> int:
        """Configured ceiling on the summed payload size."""
        return self._max_total_bytes

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    def __contains__(self, identifier: object) -> bool:
        """Return whether *identifier* names a live, unexpired reference."""
        try:
            self._entry(str(identifier))
        except ReferenceError:
            return False
        return True

    def stats(self) -> StoreStats:
        """Return a snapshot of store occupancy."""
        with self._lock:
            self._purge_expired_locked()
            namespaces = tuple(
                sorted({entry.reference.namespace for entry in self._entries.values()})
            )
            return StoreStats(
                count=len(self._entries),
                total_bytes=self._total_bytes,
                max_references=self._max_references,
                max_total_bytes=self._max_total_bytes,
                namespaces=namespaces,
            )

    # -- lifecycle -------------------------------------------------------
    def register(
        self,
        payload: Any,
        *,
        kind: DataKind,
        namespace: str = DEFAULT_NAMESPACE,
        ttl_seconds: float | None = None,
        provenance: Provenance | None = None,
    ) -> DataReference:
        """Store *payload* and return its :class:`DataReference`.

        The payload is validated against *kind* and frozen before storage, so a
        caller that later mutates its own array cannot change what the store
        returns.

        Raises
        ------
        ReferencePayloadError
            If *payload* is not valid for *kind*.
        StoreLimitError
            If storing it would exceed the count or byte ceiling.
        """
        frozen, shape, n_bytes = _normalise_payload(payload, kind)
        now = self._clock()
        effective_ttl = (
            ttl_seconds if ttl_seconds is not None else self._default_ttl_seconds
        )

        with self._lock:
            self._purge_expired_locked()

            if len(self._entries) >= self._max_references:
                raise StoreLimitError(
                    f"The data-reference store already holds "
                    f"{self._max_references} references, its configured maximum. "
                    f"Release references you no longer need before storing more "
                    f"data."
                )
            projected = self._total_bytes + n_bytes
            if projected > self._max_total_bytes:
                raise StoreLimitError(
                    f"Storing this payload ({n_bytes} bytes) would take the "
                    f"data-reference store to {projected} bytes, above its "
                    f"configured maximum of {self._max_total_bytes} bytes.  "
                    f"Release references you no longer need before storing more "
                    f"data."
                )

            reference = DataReference(
                kind=kind,
                uid=self._id_factory(),
                namespace=namespace,
                shape=shape,
                n_bytes=n_bytes,
                created_at=now,
                expires_at=(now + effective_ttl) if effective_ttl is not None else None,
                provenance=provenance,
            )
            self._entries[reference.identifier] = _Entry(reference, frozen)
            self._total_bytes += n_bytes
            return reference

    def reference(self, identifier: str) -> DataReference:
        """Return the metadata for *identifier* without returning the payload."""
        return self._entry(identifier).reference

    def lookup(
        self,
        identifier: str,
        *,
        expected_kind: DataKind | None = None,
        namespace: str | None = None,
    ) -> Any:
        """Return the payload stored under *identifier*.

        Parameters
        ----------
        expected_kind
            When given, a mismatch raises :class:`ReferenceKindError` instead of
            handing back data of the wrong type.
        namespace
            When given, the reference must belong to it; this is how one
            workflow is prevented from dereferencing another's data.

        Raises
        ------
        UnknownReferenceError
            Malformed identifier, or no such live reference.
        ExpiredReferenceError
            The reference existed but has passed its expiry.
        ReferenceKindError
            The reference holds a different kind of data.
        """
        entry = self._entry(identifier)
        reference = entry.reference
        if namespace is not None and reference.namespace != namespace:
            raise UnknownReferenceError(
                f"Reference {identifier!r} does not belong to namespace {namespace!r}."
            )
        if expected_kind is not None and reference.kind is not expected_kind:
            raise ReferenceKindError(
                f"Reference {identifier!r} holds '{reference.kind.value}' data, "
                f"but '{expected_kind.value}' was required."
            )
        return entry.payload

    def release(self, identifier: str) -> bool:
        """Delete *identifier*'s payload.

        Returns ``True`` when a payload was removed and ``False`` when the
        identifier named nothing (already released, expired or never existed).
        Releasing is idempotent by design: a retry after a lost response must
        not fail.
        """
        with self._lock:
            entry = self._entries.pop(str(identifier), None)
            if entry is None:
                return False
            self._total_bytes -= entry.reference.n_bytes or 0
            return True

    def clear(self, *, namespace: str | None = None) -> int:
        """Release every reference, or every reference in *namespace*.

        Returns the number of references removed.
        """
        with self._lock:
            if namespace is None:
                count = len(self._entries)
                self._entries.clear()
                self._total_bytes = 0
                return count
            doomed = [
                key
                for key, entry in self._entries.items()
                if entry.reference.namespace == namespace
            ]
            for key in doomed:
                entry = self._entries.pop(key)
                self._total_bytes -= entry.reference.n_bytes or 0
            return len(doomed)

    def purge_expired(self) -> int:
        """Remove expired references and return how many were removed."""
        with self._lock:
            return self._purge_expired_locked()

    # -- internals -------------------------------------------------------
    def _entry(self, identifier: str) -> _Entry:
        """Resolve *identifier* to its entry, enforcing expiry and parsing."""
        parse_identifier(identifier)
        now = self._clock()
        with self._lock:
            entry = self._entries.get(identifier)
            if entry is None:
                raise UnknownReferenceError(
                    f"No live data reference {identifier!r} in this server "
                    f"process.  References are held in memory, do not survive a "
                    f"server restart, and may have been released or expired; "
                    f"re-create the reference from its source file."
                )
            expires_at = entry.reference.expires_at
            if expires_at is not None and now >= expires_at:
                self._entries.pop(identifier, None)
                self._total_bytes -= entry.reference.n_bytes or 0
                raise ExpiredReferenceError(
                    f"Data reference {identifier!r} expired and has been "
                    f"released.  Re-create it from its source file."
                )
            return entry

    def _purge_expired_locked(self) -> int:
        """Remove expired entries.  The caller must hold the lock."""
        now = self._clock()
        doomed = [
            key
            for key, entry in self._entries.items()
            if entry.reference.expires_at is not None
            and now >= entry.reference.expires_at
        ]
        for key in doomed:
            entry = self._entries.pop(key)
            self._total_bytes -= entry.reference.n_bytes or 0
        return len(doomed)
