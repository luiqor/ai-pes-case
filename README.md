# ai-pes-case

## Decisions taken

Comparing reader interest in one topic across Wikipedia language editions,
down to a one-page PDF. Pageviews are not money, so the skill points at a
direction worth checking — never a market size — and prints its assumptions
and limitations beside every number.

**Pipeline.**

1. **Resolve, then stop.** Topic → Wikidata → real article titles, each
   confirmed to exist. A missing article stops the run and is asked about;
   substituting a similar one would measure something else.
2. **Two rows per language.** Article views *and* whole-edition views — for
   example, one edition may be several times larger than another, so raw
   views compare unequal things. Window: last two full years, incomplete
   month excluded.
3. **Local maths.** Audience share, growth (second half vs first, same
   calendar months), trend, seasonality. Absolute and share must agree,
   otherwise Wikipedia grew — not the topic.
4. **Deterministic confidence.** high/medium/low with reasons; thresholds
   fixed in code (reproducible, tested) and declared as assumptions. No
   p-value on 24 monthly points.
5. **One page.** A PDF that doesn't fit fails loudly; the HTML is
   kept. Two chart panels: views and share answer different questions.
6. **Answer from `analysis.json`** — both growth numbers, the grade, the
   assumptions and limitations. Never from memory.

**Why the metrics are fixed, not set per request.** What a study really
asks is never "which formula" — the answer is the same for every run — but
"is this enough for us", and only the latter differs per study. Recreating
the formulas on demand would move arithmetic into prose: two runs over
identical data could then disagree, no golden number could exist for a test,
and a model asked to define its own measure could quietly shape it towards
the wanted verdict. Fixing them makes the API→number mapping deterministic —
the same output for any model, even the cheapest one; tests pin exact
values; anyone can recompute a verdict from the exported series. The
per-study part — topic, window, ordering, thresholds, layers — lives in
`study.json`, explicit and outside the measurement.

**Criteria** — optional `study.json` blocks, off by default. `rank_by`
orders the table (a layer-bound order without its layer is refused);
`success` grades pass/fail thresholds per language *by the code*
(`criteria.verdicts` → `pl 2/3 met`; `passed: null` with a reason = not
measurable, never a failure) and renders them as an HTML block plus one PDF
line the agent quotes verbatim; `layers` (access, bot, top) supplies the
extra numbers such rules may need. Edits rerun from cache, and the raw
series ship separately for any bar outside the schema.

**Trust and repeats.** A coverage gap stops the workflow instead of being
filled in; `warnings` are surfaced; one function writes verdict and table,
so they cannot disagree; 271 offline tests pin the numbers to recorded API
responses. Follow-up queries re-run only what changed — cached stages cost
zero requests.

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
