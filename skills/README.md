# Skills

Agent skill playbooks for the msmcp project — version-controlled with the code
they describe, so a convention that binds every agent working here is reviewable
rather than living in one person's editor.

## Layout

```
skills/
├── README.md              # this file (catalog conventions)
├── {skill_name}/
│   ├── SKILL.md           # playbook (HARD CAP: 15 KB)
│   └── references/        # oversized reference material (Level 2 assets)
└── .archive/              # skills unused for >90 days
```

`.archive/` appears when there is something to archive; git does not track empty
directories, so it is created with its first occupant.

## SKILL.md frontmatter

Every active skill must start with YAML frontmatter containing:

```yaml
---
name: skill-name
description: One-line summary of when to use this skill.
category: category-name
version: 1.0.0
---
```

## Maintenance rules

These are conventions for the directory, **not automated checks**. Nothing in
the repository enforces them today: the gate does not read `skills/`, and the
age-based rules need an invocation log that does not exist — file dates record
edits, not uses. Until there is one, moving a skill to `.archive/` is a manual
decision, and the size and catalogue budgets are kept by whoever writes the
skill.

- Catalog token footprint (Level 0 index): <= 3,000 tokens.
- `SKILL.md` files: <= 15 KB each. Move larger reference material into `references/`.
- Skills not invoked in >30 days are flagged as stale.
- Skills not invoked in >90 days move to `.archive/`.
- Overlapping skills should be merged into a single playbook.
