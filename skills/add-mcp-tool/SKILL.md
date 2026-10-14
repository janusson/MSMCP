---
name: add-mcp-tool
description: How to add, change, or document an MCP tool in msmcp, including its wire schema, annotations, registration, and tests.
category: development
version: 1.0.0
---

# Adding or changing an MCP tool

Use this when adding a tool, changing a parameter, or editing a tool's
description. The wire contract is what the host LLM sees, and the test suite
enforces it, so get the schema right the first time.

## Where tools live

One module per logical group under `src/msmcp/tools/`:

| Module | Tools |
|---|---|
| `io.py` | `load_mzml_summary`, `list_spectra`, `load_spectrum`, `summarise_reference`, `release_reference` |
| `chem.py` | `predict_adduct_offset`, `annotate_isotopes` |
| `similarity.py` | `validate_precursor`, `compute_cosine` |
| `qc.py` | `generate_qc_summary` |
| `search.py` | `search_library`, `check_search_status`, `cancel_search` |

`server.py` owns `ping` and the `MCPServer` instance.

## The pattern

Each module exposes `register_tools(mcp)`; tools are closures inside it:

```python
def register_tools(mcp: Any) -> None:
    @mcp.tool(
        title="Short display title",
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False),
    )
    def my_tool(
        file_path: Annotated[
            str,
            Field(description="Shown to the host on the wire."),
        ],
    ) -> str:
        """Docstring becomes the tool description."""
        _ = MyToolInput(file_path=file_path)  # validate in the body
        ...
```

Register a new module in `server.py` by importing its `register_tools as
_register_*_tools` and calling it alongside the existing ones.

## Two rules that are easy to get wrong

1. **Parameter descriptions must live on the signature, not only on the
   Pydantic model.** The MCP SDK builds `inputSchema` from the function
   signature; a description that exists only on the validating model never
   reaches the host. `tests/test_tool_schemas.py` fails if a parameter has no
   wire description.
2. **Numeric constraints must be duplicated.** Declare bounds with
   `Annotated[..., Field(ge=..., le=...)]` on the signature *and* on the
   Pydantic model that validates in the body. The model is the enforcement; the
   signature is the advertisement. The same test asserts they match.

## Annotations

Every tool must declare `ToolAnnotations`. Set `open_world_hint=False` always:
the server makes no network calls. Set `read_only_hint=False` only for the
state-mutating tools, which are exactly `release_reference` and
`cancel_search`. Do not mark a read-only tool as writing.

## Description hygiene

Tool descriptions are prose for a model choosing a tool. Do not leak internal
symbols (`asyncio.`, `_run_scan`, `PathEscapeError`, ...); the schema test
rejects several of them. State behaviour and caveats, not implementation.

If a tool's result is a demonstration rather than a real identification (as
`search_library` is for a synthetic library), say so in both the description
and the result. Truthfulness of the result is a tested property.

## Tests

- Add behavioural tests next to the module's existing tests (`tests/test_io.py`,
  etc.). Tests exercise raw callables captured by the `FakeMCP` fixtures in
  `tests/conftest.py` (`io_tools`, `chem_tools`, `sim_tools`, `search_tools`,
  `qc_tools`).
- Update `MODEL_FOR_TOOL` in `tests/test_tool_schemas.py` with the new tool and
  its validating model.
- Update `EXPECTED_TOOLS` in `tests/test_smoke_stdio.py`; that set must match
  the registered surface exactly.
- Add new tools to `WRITING_TOOLS` in the schema test only if they mutate
  server-side state.

## Checklist

- [ ] Module defines `register_tools(mcp)` with the tool as a closure.
- [ ] Every parameter has an `Annotated[..., Field(description=...)]`.
- [ ] Constraints appear on both the signature and the model.
- [ ] `ToolAnnotations` set, with `open_world_hint=False`.
- [ ] Module registered in `server.py`.
- [ ] `MODEL_FOR_TOOL` and `EXPECTED_TOOLS` updated.
- [ ] `make test` and `make lint` pass.
