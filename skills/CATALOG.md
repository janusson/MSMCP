# Skill catalog (Level 0)

Compact index of the active playbooks in `skills/`. Load this before loading an
individual `SKILL.md` to decide which one applies.

Budget: the entries below must stay under 3,000 tokens.

| Skill | Category | Version | Use when |
|---|---|---|---|
| [add-mcp-tool](add-mcp-tool/SKILL.md) | development | 1.0.0 | Adding or changing an MCP tool, its schema, or its wire description |
| [ms-data-ingestion](ms-data-ingestion/SKILL.md) | data | 1.0.0 | Reading or adding support for mzML, MGF, or imzML input |
| [security-boundary](security-boundary/SKILL.md) | security | 1.0.0 | Any code path that resolves a user-supplied filesystem path |
| [data-references](data-references/SKILL.md) | data | 1.0.0 | Moving large spectra between tools without crossing MCP/JSON |
| [instrument-class-defaults](instrument-class-defaults/SKILL.md) | data | 1.0.0 | Touching an experimental threshold or its provenance |
| [quality-gate](quality-gate/SKILL.md) | workflow | 1.0.0 | Before declaring any task complete |

## Conventions

- Every `SKILL.md` carries YAML frontmatter with `name`, `description`,
  `category`, and `version`.
- `SKILL.md` files stay under 15 KB. Larger material belongs in the skill's
  `references/` directory.
- Skills unused for more than 90 days move to `skills/.archive/`.
