# ai-pes-case

## Skills

| Skill | Location |
|---|---|
| `wikipedia-interest-analyzer` | [`skills/wikipedia-interest-analyzer/SKILL.md`](skills/wikipedia-interest-analyzer/SKILL.md) |

Compares reader interest in a topic across Wikipedia language editions and
renders a one-page report with a confidence grade. Full workflow lives in
`SKILL.md`.

## Install the skill

The skill ships with its dependencies, which it installs on first use — you
only need to place `SKILL.md` where your agent looks for skills.

**Claude Code** — symlink or copy the folder:

```bash
# project (commit the symlink)
ln -s ../../skills/wikipedia-interest-analyzer .claude/skills/wikipedia-interest-analyzer

# or personal (all your projects)
ln -s /full/path/to/ai-pes-case/skills/wikipedia-interest-analyzer ~/.claude/skills/wikipedia-interest-analyzer
```

**Any Agent-Skills tool** (OpenCode and friends) — put it in one of:

```
.opencode/skills/wikipedia-interest-analyzer/SKILL.md     # OpenCode, project
.agents/skills/wikipedia-interest-analyzer/SKILL.md       # generic, project
~/.config/opencode/skills/wikipedia-interest-analyzer/SKILL.md   # OpenCode, global
~/.agents/skills/wikipedia-interest-analyzer/SKILL.md             # generic, global
```

Tools that discover `skills/*/SKILL.md` at the repo root (OpenCode) find it
automatically — clone and go.

**Verify:** ask your agent "what skills are available?" —
`wikipedia-interest-analyzer` should be listed. In Claude Code run `/skills`;
if the folder was created mid-session, run `/reload-skills` once.
