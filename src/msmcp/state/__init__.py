"""Server-side state: data references and their lifecycle.

The :mod:`~msmcp.state.pointers` module defines the reference model and the
registry; :mod:`~msmcp.state.store` owns the process-wide instance the MCP
tools share.  Nothing in this package reads files or performs science: it is
the boundary that lets a scientific payload stay on the server instead of
being serialised into an MCP response.
"""

from msmcp.state.pointers import (
    DataKind,
    DataReference,
    ExpiredReferenceError,
    PointerStore,
    ReferenceError,
    ReferenceKindError,
    ReferencePayloadError,
    StoreLimitError,
    StoreStats,
    UnknownReferenceError,
    parse_identifier,
)

__all__ = [
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
