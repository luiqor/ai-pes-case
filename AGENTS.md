# AGENTS.md

## Specifications

- **Task:** `knowledges.local/TASK.md` — the assignment: build a standalone Agent Skill that analyzes Wikipedia pageview data, generates charts, and produces short shareable reports (e.g., a one-page PDF) to help B2C founders decide which topics/languages to invest in.
- **Agent Skill spec:** `knowledges.local/agent-skill.spec.md` — the complete format specification for Agent Skills (directory structure, `SKILL.md` frontmatter and body, optional `scripts/`, `references/`, `assets/` directories, progressive disclosure, file references, validation).

Online documentation index: https://agentskills.io/llms.txt

## Research notes

- **Page view API:** `skills/wikipedia-interest-analyzer/references/api.md` — verified request recipes for all
  Wikimedia Page view analytics endpoints (base URL, required User-Agent, path parameter enums),
  plus observed failure modes (the two distinct 404 bodies, `top` path shape, full-month rule,
  omitted zeros, country-data bucketing) and an explicit list of what was *not* verified. Build the
  skill's data-fetching code on these notes rather than re-deriving the API shape. (Moved here from
  `knowledges.local/` when the skill was built — this is now the skill's own `references/api.md`,
  and it is the only copy.)
- **API traps that corrupt conclusions:** `skills/wikipedia-interest-analyzer/references/edge-cases.md`
  — includes one found during implementation: the *final* bucket of a per-article range is
  truncated to the range end date, so a study ending exactly at a month silently loses that month.

## Rules

All files in `rules/` are binding rules. Read and follow every rule in that folder before acting.
Start with `rules/no-guessing.md`.

## Key constraints

- Deliverable must be a standalone skill in the Agent Skills format, containing `SKILL.md` plus its own code doing meaningful data-processing work (Markdown-only or "agent writes the code each time" is not sufficient).
- No compiled executables; dependencies and environment setup must be reproducible. All custom materials and code live within the skill directory.
- No required tech stack (Python, TypeScript, Go, etc. are all fine).
- Must work efficiently on a fast, inexpensive tool-capable model (e.g., Claude Haiku 4.5 or comparable); test the complete workflow on such a model.
- Recommendations and reports must be grounded in data, with assumptions and limitations made explicit.
