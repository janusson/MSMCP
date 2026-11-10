---
name: untrusted-report-text
description: Render file-derived values in MSMCP tool reports without letting them reshape the Markdown or impersonate instructions.
category: security
version: 1.0.0
---

# Untrusted report text

Every MSMCP tool returns Markdown, and the MCP host feeds that Markdown to a
model. Values that come out of a caller-supplied file are therefore input to
that model's context, not decoration. A `Name:` line in an MSP library, a
spectrum path, or a digest can carry a pipe, a newline, a backtick, raw HTML, or
"ignore previous instructions".

## The rule

Before a file-derived value enters a report, pass it through `msmcp.reporting`.

| Context | Helper | Why |
| --- | --- | --- |
| Prose, banner, footer, blockquote | `escape_inline(value)` | collapses newlines and control characters, truncates, entity-escapes `& < >`, replaces backticks |
| A `|` table cell | `escape_table_cell(value)` | the above, then escapes `\` and `|` so the value cannot open or close a column |
| A block with legitimate newlines (JSON provenance) | `fenced_code(payload, language=...)` | the fence grows past the payload's longest backtick run |

Wrap the whole report in `UNTRUSTED_DATA_NOTE` the first time it quotes file
text. It tells the host, in band, that the quoted values are data.

## What counts as file-derived

- library headers: `Name`, `Formula`, `PrecursorMZ`, any MSP/MGF field
- acquisition paths, including the basename and anything from `SourceRef`
- digests (hex in practice, but they arrive through the same struct)
- anything echoed from a query reference identifier

Numbers you formatted yourself (`f"{score:.4f}"`), enum members
(`source.format`), and literals are safe. When unsure, escape; the helpers are
cheap and idempotent enough for a report line.

## Anti-patterns

```python
# wrong: the name can close the row and start a new one
lines.append(f"| {i} | {hit['compound_name']} | {hit['score']:.4f} |")

# right
lines.append(
    f"| {i} | {escape_table_cell(hit['compound_name'])} | {hit['score']:.4f} |"
)
```

```python
# wrong: a path with ``` closes the fence early
report += f"```json\n{json.dumps(provenance, indent=2)}\n```"

# right
report += fenced_code(json.dumps(provenance, indent=2), language="json")
```

## Verifying a change

Add or extend a regression test with a hostile value rather than a benign one:

- a compound name containing `|` and "IGNORE PREVIOUS INSTRUCTIONS" must leave
  the row's unescaped pipe count unchanged
- a path containing `\n> ` must not produce a line starting with `> `
- a path containing backticks must not multiply the backticks in its code span

`tests/test_reporting.py`, `tests/test_library.py::TestRealLibrarySearch`,
`tests/test_search.py::TestUntrustedReportText`, and
`tests/test_qc.py::test_qc_file_path_cannot_inject_markdown` are the models.

## Limits

Escaping protects report structure and the host's prompt. It does not sanitise
the data itself: a compound name that says "this is caffeine" is still rendered,
because suppressing it would falsify the report. The note is what tells the
reader (and the model) how to treat it.
