---
name: security-boundary
description: How every msmcp filesystem access must pass through the security boundary, and the error hierarchy that reports boundary violations.
category: security
version: 1.0.0
---

# Security boundary

Use this for any code path that resolves a user-supplied path or a resource
limit. **Every tool that touches the local filesystem must pass its paths
through `msmcp.security`.** This includes acquisition files and spectral library
files alike; a library is not exempt.

## The three hard limits

Enforced by `SecurityPolicy` (`src/msmcp/security.py`):

| Limit | Default | Override |
|---|---|---|
| Allowed root | process working directory | `MSMCP_ALLOWED_ROOT` |
| Max file size | 500 MiB | `MSMCP_MAX_FILE_SIZE_BYTES` |
| Max spectra per request | 1,000 | `MSMCP_MAX_SPECTRA` |

`DEFAULT_POLICY` reads the environment once at import time. Build a custom
policy with `SecurityPolicy.from_env(**overrides)` when a test or embedder needs
different limits; break-glass precedence is overrides, then environment, then
defaults.

## Resolution rule

`resolve_path` makes the path absolute, expands `~`, and **resolves symlinks
before the containment check**. A symlink inside the root that points outside
it is rejected, not followed. Never hand a raw user path to `open()`.

Then call `validate_file_size` and `validate_spectrum_count` as appropriate.

## Error hierarchy

Tools raise typed errors so the host can route the next step. Put a new error
in the right place:

```
MsmcpError (errors.py)
├── InaccessiblePathError       # missing, not a file, no permission
├── MalformedFileError          # opened but does not parse
├── MissingDependencyError      # optional package absent
├── UnsupportedFormatError      # recognised but not supported
├── SpectrumIndexError          # no spectrum at the requested index
└── SecurityError (security.py)
    ├── PathEscapeError         # resolved outside the allowed root
    ├── FileSizeExceededError   # exceeds the byte limit
    └── SpectrumCountExceededError
```

Do not return a generic "analysis failed" string. The point of the hierarchy is
that the *why* survives to the caller.

## Resource limits beyond files

The server-side store (`state/store.py`, `state/pointers.py`) has its own
bounds, enforced by **refusing** new registrations rather than evicting live
data — a resource error must never become a wrong answer:

- `MSMCP_MAX_REFERENCES`
- `MSMCP_MAX_TOTAL_BYTES`
- `MSMCP_REFERENCE_TTL_SECONDS` (default one hour)

A `StoreLimitError` is the expected outcome when a limit is hit.

## Posture reminders

- The server is offline and self-contained: no network, no credentials, no
  hosted service. `open_world_hint=False` on every tool.
- There is no direct instrument control and no safety interlock layer in this
  repository, by design.

## Tests

`tests/test_security.py` covers the boundary directly; `tests/test_pointers.py`
covers the store limits. When adding a limit or an error, add a case that
asserts the specific typed error, not just that something raised.
