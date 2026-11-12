# Security policy

## Supported versions

`1.0.2` is the current release and the only version on PyPI. Security fixes ship
in a new release; earlier tags, including `1.0.0`, are not patched. If you are
running from a source checkout, report against the commit you built from and say
which one it is.

## Reporting a vulnerability

Use GitHub's private vulnerability reporting for this repository:

**https://github.com/janusson/MSMCP/security/advisories/new**

That channel is enabled for this repository, and a report stays private between
you and the maintainer until an advisory is published. There is no separate
security email address, so please do not wait for one to appear.

Do not open a public issue, pull request or discussion for a suspected
vulnerability, and do not put a working exploit, real instrument data or
credentials into a public thread. If private reporting is unavailable to you for
some reason, open a public issue that says only that you have a security report
and asks for a private channel — with no details attached.

## What to include

- The version or commit you tested, the platform, and the Python version.
- The tool or module involved, and the smallest input that triggers the problem.
  A synthetic `.mzML`/`.mgf`/`.msp` fixture is enough; redact real sample data.
- What you expected, what happened, and the exact error text.
- Your assessment of impact, and whether you want credit in the advisory.

## What is in scope

MSMCP is a local MCP server that speaks JSON-RPC over stdio and reads
caller-supplied files, so the attack surface is the files it parses and the
boundary that confines them — not a network listener, because there is none.

- **Untrusted file parsing** — the mzML/MGF readers (`src/msmcp/mzml.py`,
  `src/msmcp/mgf.py`), MSP/NIST library text (`src/msmcp/library.py`), and the
  optional imzML path (`src/msmcp/massflow_io.py`). Crashes that leak a stack
  trace or a filesystem path, unbounded memory or CPU growth, and compressed
  input that defeats the inflated-byte budget are all reportable.
- **The filesystem boundary** — `src/msmcp/security.py`: any path that reaches a
  file outside `MSMCP_ALLOWED_ROOT`, including through a symlink or a paired
  imzML `.ibd` payload, and any way to exceed `MSMCP_MAX_FILE_SIZE_BYTES` or
  `MSMCP_MAX_SPECTRA`.
- **Report rendering** — file-derived text (compound names, formulae, paths,
  digests) that escapes the escaping in `msmcp.reporting` and can inject Markdown
  structure or instructions into a report an LLM host reads.
- **Packaging and supply chain** — a reachable vulnerability in a pinned
  dependency, or a defect in the published metadata or lockfile that makes an
  install resolve to something other than what the lock names.

## What is out of scope

- The scientific correctness of a result, a similarity score or a reported
  significance. Those are stated limitations with their own documents, not
  vulnerabilities: see [`SCIENTIFIC_VALIDATION.md`](SCIENTIFIC_VALIDATION.md) and
  [`VALIDATION_SIGNIFICANCE.md`](VALIDATION_SIGNIFICANCE.md).
- Anything that requires the attacker to already control `MSMCP_ALLOWED_ROOT`,
  the MCP host's configuration, or the operator's machine — by design, anyone
  who can drive the host LLM can drive the server.
- Instrument control and hardware actuation. Neither is implemented in this
  repository.
- Scanner output with no demonstrated impact, and reports against an unreleased
  working tree that the tag does not contain.

## What happens next

Reports are triaged in the private advisory: you get an acknowledgement, a
question if something is missing, and a clear accepted-or-not decision. An
accepted report is fixed on a branch, released, and published as a GitHub
security advisory that credits you if you asked for it.

This is a single-maintainer project, so no response time is promised here, and
nothing in this file is a service-level commitment. A slow reply is not a
decision either way.

[`SECURITY-REVIEW.md`](SECURITY-REVIEW.md) is an internal review of this
codebase, not a policy and not a disclosure channel.
