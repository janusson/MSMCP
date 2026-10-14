---
name: data-references
description: The server-side data reference lifecycle in msmcp that keeps large spectra out of MCP/JSON, and the invariants a new reference kind must uphold.
category: data
version: 1.0.0
---

# Server-side data references

Use this when moving data between tools, or when adding a new reference kind.
A single LC-MS/MS run has far more peaks than fit in a context window; the
reference is how MSMCP lets an agent reason *about* data without holding it.

## The lifecycle

```
load_spectrum(file)          -> "ptr:spectrum:<id>"   # a few dozen tokens
summarise_reference(ref)                              # no re-read of the file
release_reference(ref)                                # deterministic cleanup
```

`load_spectrum` registers the parsed spectrum and returns **metadata plus
provenance, never the peaks**. The payload stays in `state/store.py` behind an
opaque handle of the form `ptr:<kind>:<hex>`.

## Invariants

- **Opaque identifier.** Generated from `secrets.token_hex`. Never a Python
  `id()`, a memory address, or a path. Stable for the reference's lifetime and
  never reused after release.
- **Metadata, not payload.** A reference carries kind, shape, byte size,
  creation/expiry times, namespace, and provenance. `DataReference.to_dict()`
  is what a tool returns, and it cannot contain the payload.
- **Immutable payload.** `register` validates the payload against its kind and
  takes one defensive copy with read-only buffers, so neither the caller nor a
  later reader can mutate stored data in place. This is a deliberate one-time
  copy; MSMCP does **not** claim zero-copy.
- **The guaranteed property** is that the payload is never serialised through
  MCP/JSON. A 100,000-peak spectrum reaches the model as a small JSON object.
  This property is tested.
- **Bounded and non-evicting.** Limits are enforced by refusing new
  registrations, never by silently dropping live data.
- **Expiring and releasable.** References expire lazily on access; release is
  immediate and idempotent.

## Limits

- `MSMCP_MAX_REFERENCES`
- `MSMCP_MAX_TOTAL_BYTES`
- `MSMCP_REFERENCE_TTL_SECONDS` (default one hour)

Hitting a limit raises `StoreLimitError`.

## Errors

`state/pointers.py` defines `ReferenceError` and its subtypes, all under the
`MsmcpError` base:

- `UnknownReferenceError` — the handle was never issued or was already released.
- `ExpiredReferenceError` — the handle outlived its TTL.
- `ReferenceKindError` — the handle's kind does not match the expected kind.
- `ReferencePayloadError` — the registered payload failed validation.
- `StoreLimitError` — a store limit was reached.

## Adding a reference kind

1. Add the kind to `DataKind` in `state/pointers.py`.
2. Extend payload normalisation (`_normalise_payload` and its `_as_*` helpers)
   to validate and copy the new shape.
3. Return `DataReference.to_dict()` from the tool, never the payload.
4. Add tests in `tests/test_pointers.py` for registration, kind mismatch,
   expiry, release idempotency, and the store limits.

## Provenance

A reference's payload is registered alongside a `Provenance` record
(`provenance.provenance_for`), and a derived reference records its parents as
plain identifier strings. The store depends on provenance; provenance does not
depend on the store. Keep that direction.
