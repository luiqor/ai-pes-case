# Evolution of the `wikipedia-interest-analyzer` skill

> Deliverable writeup for `TASK.md`. Covers (1) what shipped, (2) how it evolved
> during development — including what changed from the approved plan and why,
> (3) how the output was evaluated, and (4) how I would iteratively evolve it to
> support more complex research and larger volumes of data (TASK line 23).
>
> Per `rules/no-guessing.md`, every factual claim below is marked by how it was
> obtained: **[verified]** = read from a file, produced by a tool, or observed
> live; **[proposal]** = a plan, not yet a fact.

---

## 1. What shipped **[verified]**

`skills/wikipedia-interest-analyzer/` — 37 files, ~418 KB (excluding `cache/`
and `.venv/`):

| Area | Files | Lines |
|---|---:|---:|
| `scripts/` (7 modules) | 7 | 2,578 |
| `tests/` (7 modules + `conftest.py` + `record_fixtures.py`) | 9 | 1,503 |
| `tests/fixtures/` (recorded live responses) | 13 | — |
| `references/` (`api.md`, `methods.md`, `edge-cases.md`) | 3 | — |
| `assets/report_template.html` | 1 | — |
| `SKILL.md`, `pyproject.toml`, `uv.lock`, `.gitignore` | 4 | — |

* Stack: Python 3.12 + `uv`, pinned by `uv.lock` — matplotlib 3.11.2, pandas
  3.0.6, requests 2.34.2, reportlab 5.0.1, pytest 9.1.1. **No compiled
  executables.** No SciPy/NumPy dependency for the statistics (OLS is ~10 lines).
* `SKILL.md`: 204 lines, ~2.6k tokens (spec guidance: < 500 lines / ~< 5k).
* Pipeline: `resolve → fetch → analyze → chart → report`.
* CLI: `init`, `resolve`, `status`, `override`, `all [--stage …]`, `clear-cache`.
* Offline suite: **113 tests in ~8 s**, no network required.
* `skills-ref validate` → **"Valid skill"**, exit 0.

The skill does the data work itself; the agent's job is orchestration and
interpretation. That split is what makes it usable on a cheap model.

---

## 2. How it evolved during development

### 2.1 Investigate the API before writing code **[verified]**

All five relevant endpoints were exercised live *before* implementation, and the
notes became `references/api.md`. Verified: base URL, mandatory `User-Agent`,
path parameter enums, the two distinct 404 bodies, omitted zeros, the full-month
rule, and `top`'s 5-segment path shape.

Deliberately **not** done: the numeric rate limit was never probed. The access
policy says enforcement is client-dependent and happens at the storage layer, so
measuring it would have produced a number that does not generalize. The code
takes the conservative path instead (sequential, 0.5 s delay, backoff on 429/5xx).

### 2.2 The biggest correctness fix: the truncated final month **[verified]**

While wiring the fetcher I found that a per-article monthly range **whose end
date is a month returns only that month's first day**. Repeated 3/3, same
minute, uncached:

| Request ends | `2026-08` value for cs `Přerušovaný půst` |
|---|---:|
| `…/2024100100/2026080100` | **2** |
| `…/2024100100/2026090100` | **119** (complete month) |

The `aggregate` endpoint did *not* show this (51,467,770 either way), so the two
endpoints disagree about what the end bound means.

This mattered twice: it silently dropped the last month of every study (enough
to move a growth figure by several points), and it retroactively reinterpreted an
earlier observation of mine — "September isn't loaded yet" was really "September
*1st* only".

**Fix:** `FETCH_OVERSHOOT_MONTHS = 1` — request `until + 1 month` for both series
and discard the extra bucket. **Regression test** asserts August = 119, not 2.

**Evolution in thinking:** the plan treated the API as documented; the build
treated it as *observed*. That difference is now encoded in
`references/edge-cases.md` so it survives me.

### 2.3 A threshold rule that failed its own output **[verified]**

Plan: penalise confidence when seasonality is present. Implementation reported
`reliable: false` for the window *and* downgraded on the same number — a 2.042
peak ratio against a 2.0 threshold flipped Polish to `low` on a window the same
output called unreliable.

**Change:** the penalty now gates on `reliable` (≥ 24 months / ≥ 2 cycles), while
an observed-but-unreliable peak is still surfaced as `seasonality_observed` and
in the limitations. Claiming a downgrade from an estimate we just labelled
unreliable was incoherent; describing it without penalising it is not.

### 2.4 The alignment rule I had written down wrong **[verified]**

Extending a live study to 44 months made confidence *worse* — Polish fell to
`low`, Czech/German to `medium`. Cause: `aligned = (n − ⌊n/2⌋) % 12 == 0`.

| months | 12 | 23 | 24 | 35 | 36 | 44 | 47 | 48 | 72 |
|---|---|---|---|---|---|---|---|---|---|
| aligned | ✗ | ✓ | ✓ | ✗ | ✗ | ✗ | ✓ | ✓ | ✓ |

My `methods.md` had claimed alignment fails when the window "is not a multiple of
12" — plainly wrong (12 fails, 23 passes). The implementation was right and the
prose was wrong, so the prose got corrected against measured behaviour, and the
rule is now pinned by `test_alignment_follows_the_offset_rule`.

Two follow-on fixes, because "longer ≠ better" is counter-intuitive:

* `resolve_window` now **warns at `init`/`status` time** — before any fetch —
  that an unalignable window caps confidence at `medium`, and names 23/24/47/48.
* `SKILL.md`'s "longer window" follow-up row says the same thing.

This is also the justification for the default being **24** and not 12: twice the
data *and* the only shorter length that splits into two whole calendar years.

### 2.5 Policies that were added rather than planned **[verified]**

| Change | Why |
|---|---|
| `native_gap` recorded on substitutions | An `override` must not erase the fact that the edition has no article of its own. Surfaced in the headline, limitations and JSON. |
| `common.configure_console()` | Windows `cp1252` crashed the first end-to-end run while merely *printing* `Přerušovaný půst`. Every entry point now forces UTF-8. |
| Confirm-before-use on every title | I hand-typed `Głódówka`; the real page is `Głodówka`. They looked identical, `prop=info` said `missing`, and an earlier conclusion of mine ("stale search index") was simply a typo. The filter was right; my typing was not. Rule: **never hand-write a title.** |
| One encoder, `common.encode_title` | Tests assert the encoded URL reproduces a recorded production URL byte-for-byte, so encoding cannot drift into two places. |
| Fonts from the matplotlib wheel | PDF base-14 fonts have no `ř š ů ł ę`. Registering DejaVu from inside the pinned wheel means no host-font dependency on any platform. |
| Overflow refuses rather than spills | Layout reserves space *before* drawing; if it cannot fit one page, `ReportOverflow` fires, HTML is written, **no partial PDF is left**. |

### 2.6 The cheap-model test drove seven fixes **[verified]**

The complete workflow was run end-to-end by a fresh-context subagent on the free
model (`opencode/mimo-v2.6-flash-free`; no model override passed, so it inherited
the session's model), asked to answer TASK example #2 — astronomy in Ukrainian vs
English. It reported friction honestly, and almost all of it was real:

| # | Finding | Fix | Guard |
|---|---|---|---|
| F1 | `status` and `SKILL.md` told the user to run `run.py resolve`, **which did not exist**; it had to guess `all --stage resolve` from `--help` | real `resolve` subcommand (review-before-fetch) | `test_resolve_subcommand_exists…` |
| F2 | no `--study`/`--out` in *any* example → following `SKILL.md` verbatim writes into the skill dir | every example now passes both; "treat the skill directory as read-only" | `test_init_hints_point_at_real_commands` |
| F3 | `cache/` is not moved by `--out` and was undocumented | documented explicitly | — |
| F4 | `SKILL.md` promised `search_hits`; `status` never printed them | prints `hits:` with a `->` marker and the `--qid` remedy | `test_status_lists_runner_up_concepts…` |
| F5 | no decision rule when *every* language declines; `fastest_growth` was misread as growth | ranked tiebreak in `SKILL.md`, plus "`fastest_growth` means *least bad*" | — |
| F6 | User-Agent described as both optional and required | reworded: always sent; `WIA_USER_AGENT` adds your contact | — |
| F7 | `window: (default) .. (default)` is unreadable | resolves and prints the real dates + window warnings | `test_status_shows_the_effective_window…` |

The most valuable guard came out of F1: a **meta-test** that scans `run.py` for
every `run.py <word>` string and asserts it is a real subcommand. That class of
bug — documentation pointing at a command that was never defined — can no longer
ship.

### 2.7 Second pass: the fixes held — and surfaced two more **[verified]**

A second fresh-context run (different query, TASK example #3, three language
editions) confirmed **all seven fixes**, then found five more. The point of
running it twice is that the first pass only tells you what was *wrong*; the
second tells you whether fixing it was enough.

| # | Finding | Fix | Guard |
|---|---|---|---|
| **N1** | The new confidence-cap warning reached `init`, `all` and `analysis.json` — **but not the HTML/PDF**. A reader of the shareable one-page report could not tell its confidence had been capped. | one `caveat_items()` source renders warnings → limitations → assumptions in *both* renderers | `test_report_renders_runtime_warnings…` |
| **N2** | The documented `WIA_USER_AGENT` line was **cmd.exe syntax**; verbatim in PowerShell it threw `CommandNotFoundException` (and `set` is `Set-Variable` anyway). | `$env:WIA_USER_AGENT=…` / `export …`, with an explicit "not `set VAR=`" note | — |
| N3 | Candidate snippets cut mid-word (`… Diese Pro)`) | `common.clip()` breaks on a word boundary | `test_clip_cuts_on_a_word_boundary` |
| N4 | The growth definition was stated in *both* `limitations` and `assumptions`, printing the same bullet twice and spending scarce one-page space | removed the duplicate at source; `caveat_items()` also drops exact repeats | `test_report_does_not_print_the_growth_bullet_twice` |
| N5 | `resolve` advertised `--out` though it writes no results | flag removed | `test_resolve_has_no_out_flag…` |

**N1 was the important one.** It is the same failure mode as the original
"assumptions and limitations must be made clear" requirement, one layer further
out: a caveat that exists only in a JSON file is not disclosed to the person
holding the PDF. Fixing it also exposed a useful interaction — the new
exact-duplicate filter silently collapsed my own overflow tests (which used 20
*identical* strings to force an overflow), so those tests were rewritten with
distinct content, which is what a genuinely over-long report looks like.

Not changed, by design: `all` still reports a gap once per stage (`resolve` and
`fetch`), because each stage can be run alone and must stand on its own.

---

## 3. How the output was evaluated

**a. Golden numbers, verified before they were frozen.** Hand-checked against
production *first* (pl `Post`: 38,863 total, −17.3% absolute, −8.7% share; cs
`Přerušovaný půst`: 6,546 total, −55.7%, −49.0%), then recorded as fixtures. Any
drift in the maths, the window handling or the encoding now fails the suite.

**b. The gap policy is tested at every layer.** Unit (`page_exists`, stale-hit
dropping) → resolution (gap + candidates, no substitution) → fetch (`gaps`,
`native_gap`) → analysis (limitations, headline) → report (a visible
`pl | no article exists | not measurable` row) → CLI
(`test_pipeline_reports_a_gap_end_to_end`). A misspelled override fails loudly
rather than reporting zero interest.

**c. The one-page rule is structural, not aspirational.** Both the fitting and
the overflowing cases are tested; overflow leaves no partial PDF. Live runs with
2 and 3 languages each produced exactly 1 page (checked by scanning the PDF for
`/Type /Page` — the spec forbids adding a PDF library the skill does not need).

**d. Follow-up refinement works and is cheap.** Live: added `de` and moved the
window to 44 months → **7.7 s**; immediate rerun unchanged → **3.5 s** with zero
requests (disk cache). The `pl = Post` override survived the manifest edit, and
the PDF still fit one page with three languages.

**e. The cheap-model runs** (the constraint TASK line 31 imposes) — run **twice**,
on the free model `opencode/mimo-v2.6-flash-free` (no model override passed, so
each fresh-context subagent inherited the session's model):

* **Pass 1** — TASK example #2 (astronomy, uk vs en): ~10 tool-call rounds,
  **5 skill commands**, under 10 minutes, reading only `SKILL.md` plus two
  `--help` invocations. It never opened the references or the chart and answered
  entirely from `analysis.json`. Every JSON key `SKILL.md` named existed
  verbatim. Its answer was correct *and appropriately hedged* — both languages
  declining, English chosen on confidence + scale, with the Ukrainian counter-case
  (3.7× the relative attention) volunteered rather than hidden. It produced the
  7 defects in §2.6. Its blind spot: it never exercised the coverage-gap path,
  because both articles resolved.
* **Pass 2** — TASK example #3 (learning English across pl/de/uk): ~13 turns /
  18 tool calls, again `references/` and `tests/` untouched. It **left German a
  pure gap** (candidates were irrelevant album mentions — judging them rather
  than auto-substituting is exactly Rule 2), correctly applied the all-declining
  guidance, rejected `fastest_growth = "pl"` as noise (8 views/month, R² 0.05),
  and independently counted 1 page in the PDF. It produced the 5 findings in
  §2.7, including the substantive N1.

Two passes on a cheap model found **12 real defects** that 112 unit tests, a
manual spec checklist and an official validator had all missed — every one of
them in the *interpretation and usability* layer rather than the arithmetic.
That is the strongest evidence that this was the right test to run.

**f. Spec conformance.** `skills-ref` was installed by the documented method
(fetch `agentskills.io/llms.txt` → clone the repo → `uv sync` in `skills-ref/`),
then `skills-ref validate` → **"Valid skill"**, exit 0. Backed by a manual
checklist: `name` matches the directory and matches the allowed pattern, `description`
631/1024, `compatibility` 181/500, one root `SKILL.md`, 0 compiled artifacts,
`uv.lock` present, and **no reference anywhere in the skill that points outside
the skill directory**.

---

## 4. How I would iteratively evolve it

Ordered so that each step is independently shippable and pays for the next.
Everything here is **[proposal]** unless marked otherwise.

### Stage 1 — Multi-article topic clusters (largest accuracy gain per unit work)
*Now:* one Q-item, one article per edition — an admitted proxy, and the weakest
link in "interest in **topic X**".
*Evolve:* resolve the topic to a **set** of Q-items (canonical + synonyms +
subtopics), union their articles per edition, sum views while de-duplicating
redirects/aliases so nothing is double-counted.
*Why first:* it attacks the largest source of measurement error without touching
data volume. A topic whose edition has three narrow articles currently looks
near-dead.
*Watch out:* the union must be *reported* (article count + titles per language)
or the number stops being auditable.

### Stage 2 — Daily granularity and longer horizons
*Now:* monthly only, window clamped to the 2015-07 floor.
*Evolve:* add daily series for event-level resolution (a launch, a news spike),
plus 5–10 year studies.
*Why:* monthly hides change points and forces the half-vs-half compromise.
*Must verify first* **[unverified]**: the daily path's range cap, its final-bucket
behaviour (the monthly truncation bug makes daily suspicious), and whether
`aggregate` and `per-article` disagree there too. Daily × 10 years × many
articles also means chunked requests and a real storage format (e.g. Parquet
**[proposal]**) instead of one JSON per study.

### Stage 3 — Portfolio scale: many topics × many languages
*Now:* one study at a time, flat response cache.
*Evolve:* a manifest of manifests — N topics × M languages — with a request
budget, resumable runs, and one comparable league table across topics rather than
a one-topic report.
*Why:* real research is "which of these 30 directions deserves a spike", not
"tell me about one".
*Must verify first* **[unverified]**: per-client request ceilings. The rate limit
has never been probed, deliberately. Before batching I would either (a) obtain
documented limits, or (b) run a controlled, polite measurement and record it as a
measured-not-guaranteed figure. Concurrency only after that.

### Stage 4 — Cache and storage that survive scale
*Now:* `cache/<hash>.json` flat files, adequate at tens of URLs.
*Evolve:* an index keyed by `(project, article, granularity, access, agent)`,
TTL/invalidation for recent months (they change while loading), and a
`--dry-run` that reports how many requests a study would make.
*Why:* Stage 3 without this re-fetches everything and Stage 2 without TTL makes
"last month" stale in a way nobody notices.

### Stage 5 — Statistics that the data finally justifies
*Now:* half-vs-half, OLS + R², seasonality profile; no significance testing —
explicitly because ~23 monthly points cannot support a p-value.
*Evolve:* with Stage 2/3 volumes, add change-point detection, per-series seasonal
decomposition, and block-bootstrap intervals on the growth figure.
*Why:* the honest "we cannot test this" answer becomes a quantified interval
once there are enough points. Keep the existing grades; add intervals beside them
rather than replacing the deterministic table, which is what makes results
reproducible and cheap-model-legible.

### Stage 6 — Cross-signal validation (the actual B2C question)
*Now:* pageviews, and a limitation that says in every report: **article reads,
not willingness to pay.**
*Evolve:* a second and third independent signal per topic/language — search
interest, community-mention velocity, store/category rankings — combined into a
triangulated view with each source's bias stated.
*Why:* this is the difference between "people read the article" and "people would
pay for a product". It is the highest-value direction and the one that most needs
fresh verification of each source's API, quota and licence terms **[unverified]**.
The report format already has the right shape for it: a verdict, assumptions and
limitations that must all be carried forward.

### Stage 7 — Evaluation harness and report variants
*Now:* 107 golden tests + one manual cheap-model run.
*Evolve:* a corpus of founder questions with expected claims and contradiction
checks ("does the skill ever call a declining language growing?"), re-run on the
cheap model at every change; and an optional appendix mode — keep the one-page
verdict, add clearly-separated detail pages, with the strict one-page rule
retained for the default.
*Why:* the cheap-model run was the single most productive test in this project —
it found seven real defects in one pass. It should be re-runnable on demand
rather than a one-off.

### Sequencing rationale
1 → 2 → 3 is **accuracy, then resolution, then scale**; doing them in any other
order scales up the wrong measurement. 4 must land with or before 3. 5 depends on
2/3 for statistical power. 6 is independent and highest value, but is gated on
verifying external sources. 7 should start now and run continuously.

---

## 5. Known limits and deliberately deferred work **[verified]**

* Monthly granularity; one Q-item; one article per language as proxy.
* `fastest_growth` still reads as growth when every language is declining —
  mitigated by `SKILL.md` guidance, **not yet renamed** in `analysis.json`.
* Rate limit never probed (deliberate, §2.1). `top` / `top-by-country`, titles
  containing `/` or `#`, and daily-granularity end-bucket behaviour are on the
  explicit *not verified* list in `references/api.md`.
* No significance testing — by design, `references/methods.md` §7.
* All confidence thresholds are **design parameters, not facts**, stated as
  assumptions in every report.
* Everything is local/uncommitted in the repo: `git status` shows `AGENTS.md`,
  `rules/`, `skills/` untracked and `.gitignore` modified. No commits were made.
