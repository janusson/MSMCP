"""Process-wide access to the server's data-reference store.

:mod:`msmcp.state.pointers` implements the registry; this module owns the one
instance the MCP tools share and the thin helpers they call.  Keeping the
singleton here rather than in the tool modules means the lifetime and the
limits of server-side data are configured in exactly one place.

Configuration
-------------
The store reads its limits from the environment once, at import time, matching
how the rest of the server is configured before it starts serving:

``MSMCP_MAX_REFERENCES``
    Maximum number of live references (default 128).
``MSMCP_MAX_TOTAL_BYTES``
    Maximum summed payload size (default 512 MiB).
``MSMCP_REFERENCE_TTL_SECONDS``
    Lifetime of a reference in seconds (default 3600; ``0`` disables expiry).

Namespaces
----------
The stdio server serves a single local session and therefore uses the default
namespace for everything it stores.  The underlying store isolates references
by namespace, so a future multi-session transport can partition workflows
without changing this module's callers; the mechanism is exercised by the
store's own tests.
"""

from __future__ import annotations

import os
from typing import Any, Final

import numpy as np

from msmcp.provenance import Provenance
from msmcp.state.pointers import (
    DEFAULT_MAX_REFERENCES,
    DEFAULT_MAX_TOTAL_BYTES,
    DEFAULT_NAMESPACE,
    DataKind,
    DataReference,
    PointerStore,
    StoreStats,
)

__all__ = [
    "DEFAULT_NAMESPACE",
    "ENV_MAX_REFERENCES",
    "ENV_MAX_TOTAL_BYTES",
    "ENV_REFERENCE_TTL",
    "configure_store",
    "get_store",
    "peak_list_of",
    "reference_summary",
    "release",
    "reset_store",
    "resolve",
    "resolve_peak_list",
    "resolve_spectrum",
    "stats",
    "store_embedding",
    "store_peak_list",
    "store_spectrum",
]

ENV_MAX_REFERENCES: Final[str] = "MSMCP_MAX_REFERENCES"
ENV_MAX_TOTAL_BYTES: Final[str] = "MSMCP_MAX_TOTAL_BYTES"
ENV_REFERENCE_TTL: Final[str] = "MSMCP_REFERENCE_TTL_SECONDS"

DEFAULT_REFERENCE_TTL_SECONDS: Final[float] = 3600.0
"""References expire after an hour by default, matching job retention."""


def _positive_int_from_env(name: str, default: int) -> int:
    """Read a positive integer from the environment, falling back to *default*."""
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from None
    if value <= 0:
        raise ValueError(f"{name} must be a positive integer, got {raw!r}")
    return value


def _ttl_from_env() -> float | None:
    """Read the reference TTL from the environment (``0`` means no expiry)."""
    raw = os.environ.get(ENV_REFERENCE_TTL)
    if not raw:
        return DEFAULT_REFERENCE_TTL_SECONDS
    try:
        value = float(raw)
    except ValueError:
        raise ValueError(f"{ENV_REFERENCE_TTL} must be a number, got {raw!r}") from None
    if value < 0:
        raise ValueError(f"{ENV_REFERENCE_TTL} must not be negative, got {raw!r}")
    return value or None


def store_from_env() -> PointerStore:
    """Build a store configured from the environment."""
    return PointerStore(
        max_references=_positive_int_from_env(
            ENV_MAX_REFERENCES, DEFAULT_MAX_REFERENCES
        ),
        max_total_bytes=_positive_int_from_env(
            ENV_MAX_TOTAL_BYTES, DEFAULT_MAX_TOTAL_BYTES
        ),
        default_ttl_seconds=_ttl_from_env(),
    )


_STORE: PointerStore = store_from_env()
"""The process-wide store shared by every MSMCP tool."""


def get_store() -> PointerStore:
    """Return the process-wide data-reference store."""
    return _STORE


def configure_store(store: PointerStore) -> PointerStore:
    """Replace the process-wide store, returning the previous one.

    Intended for tests and for embedders that need explicit limits; the MCP
    server itself only ever reads :func:`get_store`.
    """
    global _STORE
    previous, _STORE = _STORE, store
    return previous


def reset_store() -> None:
    """Release everything held by the process-wide store."""
    _STORE.clear()


def stats() -> StoreStats:
    """Return occupancy statistics for the process-wide store."""
    return _STORE.stats()


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------
def store_spectrum(
    spectrum: Any,
    *,
    provenance: Provenance | None = None,
    namespace: str = DEFAULT_NAMESPACE,
    ttl_seconds: float | None = None,
) -> DataReference:
    """Store a :class:`~msmcp.mzml.Spectrum` server-side and return its reference."""
    return _STORE.register(
        spectrum,
        kind=DataKind.SPECTRUM,
        namespace=namespace,
        ttl_seconds=ttl_seconds,
        provenance=provenance,
    )


def store_peak_list(
    peaks: Any,
    *,
    provenance: Provenance | None = None,
    namespace: str = DEFAULT_NAMESPACE,
    ttl_seconds: float | None = None,
) -> DataReference:
    """Store an ``(N, 2)`` peak list server-side and return its reference."""
    return _STORE.register(
        peaks,
        kind=DataKind.PEAK_LIST,
        namespace=namespace,
        ttl_seconds=ttl_seconds,
        provenance=provenance,
    )


def store_embedding(
    vector: Any,
    *,
    provenance: Provenance | None = None,
    namespace: str = DEFAULT_NAMESPACE,
    ttl_seconds: float | None = None,
) -> DataReference:
    """Store an embedding vector server-side and return its reference."""
    return _STORE.register(
        vector,
        kind=DataKind.EMBEDDING,
        namespace=namespace,
        ttl_seconds=ttl_seconds,
        provenance=provenance,
    )


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------
def resolve(
    identifier: str,
    *,
    expected_kind: DataKind | None = None,
    namespace: str | None = DEFAULT_NAMESPACE,
) -> Any:
    """Return the payload behind *identifier* from the process-wide store."""
    return _STORE.lookup(identifier, expected_kind=expected_kind, namespace=namespace)


def resolve_spectrum(identifier: str) -> Any:
    """Return the :class:`~msmcp.mzml.Spectrum` behind *identifier*."""
    return resolve(identifier, expected_kind=DataKind.SPECTRUM)


def resolve_peak_list(identifier: str) -> np.ndarray:
    """Return the ``(N, 2)`` peak array behind a peak-list reference."""
    return resolve(identifier, expected_kind=DataKind.PEAK_LIST)


def peak_list_of(identifier: str) -> np.ndarray:
    """Return an ``(N, 2)`` ``[m/z, intensity]`` array for *identifier*.

    Accepts either a spectrum reference (whose two arrays are paired up) or a
    peak-list reference, so a scorer can be handed whichever a caller happened
    to produce without the caller having to normalise first.
    """
    reference = _STORE.reference(identifier)
    if reference.kind is DataKind.SPECTRUM:
        spectrum = resolve_spectrum(identifier)
        return np.column_stack((spectrum.mz, spectrum.intensity))
    return resolve_peak_list(identifier)


def reference_summary(identifier: str) -> dict[str, Any]:
    """Return a compact, JSON-safe description of *identifier*."""
    return _STORE.reference(identifier).to_dict()


def release(identifier: str) -> bool:
    """Release *identifier*'s payload.  Returns ``False`` if it was already gone."""
    return _STORE.release(identifier)
