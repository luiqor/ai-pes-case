---
name: wikipedia-interest-analyzer
description: Measures how reader interest in a topic changes over time across Wikipedia language editions using Wikimedia pageview data, then renders a comparison chart and a paginated PDF/HTML report with an explicit confidence grade, assumptions and limitations. Use when someone asks which topics or languages to invest in, whether interest in a topic is growing or declining in a given language edition, how confident a trend is, which audience to investigate next, or wants a shareable report comparing audiences — for example "compare interest in intermittent fasting in the Polish and Czech Wikipedias over the past two years".
compatibility: Requires Python 3.12+, uv, and network access to wikimedia.org, *.wikipedia.org and wikidata.org. Charts and the PDF are produced locally (matplotlib, reportlab); no API key needed.
metadata:
  version: "1.0"
  stage: "1.0"
---

# Wikipedia interest analyzer

Turns a natural-language topic plus a list of language editions into a
data-grounded answer: **is interest in this topic growing here, and how much
should we trust that?** It fetches Wikimedia pageview data, normalises it
against each edition's total traffic, computes trend and seasonality statistics,
draws a chart and emits a report — an HTML file and a PDF that paginate, so
both carry the same content block for block.

It is deliberately sceptical of its own output. Every report carries a
confidence grade with the reasons behind it, plus its assumptions and
limitations. **A coverage gap is reported as a gap — never quietly replaced by a
similar article.**

## Step 0 — language, before anything else

Do this **first**, before `init`, before any other command:

1. **Detect the language the user prompted in.** That language is the default
   for *everything* you produce: the report (`report.pdf` / `report.html`), the
   chart labels, **and your answer in the chat**.
2. Pass it on the very first command:
   `run.py init … --report-lang <code>` (`uk` → `--report-lang uk`).
   English only when the prompt is English, or when the user explicitly asks
   for English. **Never fall back to English because it is the default.**
3. The first run in that language writes `<out>/translations.<code>.json` with
   the **English** messages and renders this run in English as a placeholder.
   You then **translate every value yourself** and rerun `all` with the same
   `--report-lang`. **Do not show the user a placeholder/English report** when
   they prompted in another language.

Before running `all`, check the manifest: `status` must print
`report: <code>` in the user's language. If the line is missing or says `en`
while the prompt was in another language, fix it **before** fetching anything —
pass `--report-lang <code>` to `init` (with `--force`, before `resolve`) or add
`"report_language": "<code>"` to `study.json`.

## Setup (once)

```bash
cd skills/wikipedia-interest-analyzer
uv sync                      # installs pinned deps from uv.lock
uv run pytest                # optional: 300+ offline tests, ~20s
```

**Treat the skill directory as read-only.** Every command takes `--study` (the
manifest) and `--out` (the results); both default to the current directory, so
omitting them writes into the skill folder. Always pass both. Throughout,
`$WORK` means any writable directory *outside* the skill (e.g.
`C:\interest-report` or `./interest-report`).

The HTTP cache is the one exception: it lives (unless `WIA_CACHE_DIR` says
otherwise) in `skills/wikipedia-interest-analyzer/cache/`, is shared between
studies and runs,
is gitignored, and is **not** moved by `--out`. Clear it with
`run.py clear-cache`.

Wikimedia requires a `User-Agent` on every request and the skill always sends
one (default `wikipedia-interest-analyzer/0.1 (set WIA_USER_AGENT to your
contact)`). For sustained or public use, set your own contact string:

```powershell
$env:WIA_USER_AGENT = "your-name/1.0 (you@example.com) requests"    # PowerShell
```

```bash
export WIA_USER_AGENT="your-name/1.0 (you@example.com) requests"     # bash / zsh
```

(Not `set VAR=…` — that is cmd.exe syntax and fails in PowerShell.)

## Workflow

### 1. Create a study

```bash
uv run python scripts/run.py init --topic "intermittent fasting" --langs pl,cs --report-lang pl --study "$WORK/study.json"
```

`--report-lang` is **not optional**: pass the language of the user's prompt
(see [Step 0](#step-0--language-before-anything-else)); it is stored in the
manifest and reused by every rerun. Omit it only when the prompt itself is in
English.

Optional `--since 2024-10 --until 2026-08` fixes the window (default: the last
24 complete months — `init` prints the resolved dates, not `(default)`). This
writes a small manifest you can edit later instead of re-querying.

### 2. Resolve the topic, then review it before fetching

```bash
uv run python scripts/run.py resolve --study "$WORK/study.json"
uv run python scripts/run.py status  --study "$WORK/study.json"
```

Resolution is **concept-first**: the topic is searched on Wikidata, a Q-item is
chosen, and that item's sitelinks give the article in each edition. `resolve`
stops right there — no pageviews are fetched — so you can review the mapping
first. `status` reprints it from the manifest.

Read the output carefully:

* `concept: Q1666254 'intermittent fasting'` — the chosen Wikidata item;
* `hits:` — the runner-up concepts, with the chosen one marked `->`. If the
  wrong item was picked, re-run `resolve --qid Q…`;
* `cs: Přerušovaný půst` — a real article was found;
* `pl: GAP` — that edition has **no article**, and candidate articles are
  listed underneath. That list is for the **user**, not for you — see step 3;
* `window: 2024-10 .. 2026-08` — the dates actually used.

### 3. Any GAP → stop and ask the user (mandatory)

If `resolve`/`status` prints a `GAP`, **do not continue to step 5 before the
user has answered**. Ask in the user's language (step 0), listing the
candidates exactly as `status` printed them:

> У **pl**-вікіпедії немає статті «intermittent fasting». Що робимо?
>
> 1. `Głodówka lecznicza` — *„FJ.F. Trepanowski … Intermittent fasting
>    combined with calorie restriction is effective for weight loss…"*
> 2. `Stres oksydacyjny` — *„…produced by caloric restriction, intermittent
>    fasting, exercise…"*
> 3. Пропустити **pl** — тоді у звіті буде «no article exists / not
>    measurable» для цієї мови.
> 4. Змінити тему/список мов.

Rules for the question:

* **Copy** titles and snippets from `status` — never invent one; a plausible
  misspelling in `override` looks like zero interest.
* If the user picks a candidate → step 4. If they pick "skip" → leave the gap
  (or drop the language from `study.json`); the report will say
  `pl | no article exists | not measurable`.
* **Only if the user cannot be asked** (no way to reply, or they already said
  "don't ask, just run it") may you continue with the pure gap — and say so in
  your answer. Never pick a candidate yourself.
* The same applies to a wrong concept: if `hits:` shows an obviously better
  Q-item, ask before resolving (`resolve --qid Q…`).

### 4. Fill the gap only after the user chose

```bash
uv run python scripts/run.py override --lang pl --title "Post" --study "$WORK/study.json"
```

This is the **only** way a substitute gets used, and the choice must be the
**user's** (step 3), never yours.
It is then disclosed in the headline, the limitations and `analysis.json`
(`resolved_via: override`, `native_gap: true`). Never invent a title — copy it
from `status` output; a plausible misspelling looks like zero interest.

If the user prefers a pure gap, skip this step. The report will show
`pl | no article exists | not measurable`.

### 5. Run everything

Before this command, all of these must be true:

- `status` prints `report: <the user's language>` **and**
  `translations.<code>.json` is translated by you (step 0);
- every `GAP` was put to the user and answered (step 3) — no silent skips;
- `table_key` is written into `study.json`, in the report's language.

```bash
uv run python scripts/run.py all --study "$WORK/study.json" --out "$WORK"
```

Stages are `resolve → fetch → analyze → chart → report`. Run one at a time with
`--stage analyze`, or rerun `all` freely — responses are cached, so an unchanged
study costs no new requests.

Outputs land in `--out` (default: the current directory):

| File | What it is |
|---|---|
| `analysis.json` | **Read this first.** Headline, per-language metrics, comparison, assumptions, limitations |
| `series.json` | Raw aligned monthly series |
| `report.pdf` | The shareable artifact — every block the HTML carries, flowed over as many pages as it needs |
| `report.html` | Same content, self-contained (chart embedded), no page limit |
| `chart.png` / `chart.svg` | Two panels: absolute views, normalised share |
| `translations.<lang>.json` | Report/chart strings for `--report-lang`; written (English) on first use, translated in place |

### Report language

Everything the user reads — the report, the chart **and your answer in the
chat** — is written in the language of their prompt, unless they ask for
another one. Pick the code yourself (`pl`, `uk`, `cs`, `de`,
`fr`, `pt-br`, … any language the agent can translate into); the skill ships
**no** translation catalogue, you supply the wording. See
[Step 0](#step-0--language-before-anything-else).

1. Pass the code: `run.py init … --report-lang pl` (stored in the manifest)
   or per run: `run.py all … --report-lang pl`. Either way the report, the
   PDF and every chart label use it. **Do not omit it** — an omitted code
   means English.
2. The first such run finds no translations and **writes
   `<out>/translations.pl.json`** — the English messages keyed by id — then
   renders this run in English with a visible note. **That run is a
   placeholder: translate before you show anything to the user.** Translate
   every value in
   `"messages"` (keep the ids and the `{placeholder}` names exactly as they
   are), then rerun (point 3). Optionally pre-write it with
   `run.py i18n-template --lang pl --out <path>` (`--force` to overwrite).
3. Rerun with the same flag. Missing ids fall back to English — the report
   then carries a visible `Warning (pl): Untranslated text: …` note naming
   them, and stderr lists every id. **Never fail and never fall back
   silently.**
4. Write the `table_key` block into `study.json` once (see below): it is the
   one part of a localised report that is *generated* rather than translated.

**Table headers are fixed English codes; the report defines them under the
table.** `Share/M`, `YoY`, `YoY share`, `R²`, `Lang`, `Article`, `Views`,
`Confidence` are printed as-is in every language and are **not** part of the
translation: they are codes, not prose, and a translated header has nowhere
to fit in a narrow column anyway. You supply the decoding **once per study**,
in the manifest (`study.json`):

```json
{
  "table_key": {
    "heading": "How to read this table",
    "Share/M": "Share of that language edition's monthly pageviews, per million",
    "YoY": "Change in article pageviews versus the same month last year, in %",
    "YoY share": "Same year-on-year move, but for the normalised share",
    "R²": "How much of the trend a straight line explains (1 = perfectly linear)"
  }
}
```

Write it in the report's language, in your own words — the report prints it
verbatim as a **table key directly under the comparison table**, in both HTML
and the PDF, so wording is yours to generate, never to look up. The keys are
the printed column names; an entry naming a column the table never prints is
warned about on stderr (the `heading` key is the block's own title). Omit the
block and the shipped English wording is used, admitted in the visible
fallback note like any other missing string.

What stays English, deliberately: `analysis.json` (the data contract your
answer is built from), `series.json`, the console progress/warning wording,
message ids everywhere, and the table headers themselves — `Share/M`, `YoY`,
`Confidence` are codes the table key then explains. Numbers, article titles,
language codes and the topic concept are never translated. The `Warning (pl):`
prefix on the fallback note is fixed English so a partially-translated report
still admits it in any language.

### Data layers and the user's own criteria (optional)

Optional blocks in `study.json` let the *user's* criteria shape the study
instead of only the defaults. All are **off by default**: a study without
them issues exactly the requests it always did and renders exactly the table
it always rendered.

```json
{
  "layers": ["access", "bot", "top"],
  "criteria": {
    "rank_by": "bot_share",
    "success": [
      {"id": "growing", "metric": "yoy_share_pct", "op": ">=", "value": -5},
      {"id": "volume", "metric": "share_ppm", "op": ">=", "value": 10}
    ]
  }
}
```

| Layer | Adds to `analysis.json` | Answers a question like |
|---|---|---|
| `access` | `metrics.<lang>.access_split` (`desktop_pct`, `mobile_pct`, …) + a `Mobile %` table column | "is this read on phones?" |
| `bot` | `metrics.<lang>.bot_share_pct` (non-user part of `all-agents` views) + `Bot %` | "how much of that is bots?" |
| `top` | `metrics.<lang>.top_rank` (`rank`/`list_size` at the window's end) + `Rank` (`>1000` = outside the list) | "is it actually among the top pages?" |

`criteria.rank_by` reorders the comparison table and prints its reason under
the table (`Ranked by …`). Values: `share_ppm`, `yoy_share`, `mobile_share`
(needs `"layers": ["access"]`), `bot_share` (needs `"layers": ["bot"]`, and
**the lowest bot share leads** — the cleanest human signal first). The
manifest refuses a layer-bound criterion without its layer, so a report can
never be sorted by a metric it never fetched; the same order is stored at
`comparison.ranked_by` for your answer.

`criteria.success` is the user's **own definition of success**: thresholds
you translate from their words. The *code* grades them per language, so the
verdict is reproducible and data-grounded instead of prose you re-derive
every turn.

| Piece | Rule |
|---|---|
| `metric` | closed list: `share_ppm`, `article_total`, `mean_monthly_views`, `yoy_share_pct`, `yoy_article_pct`, `trend_r2`, `confidence`, `mobile_pct` (needs `access`), `bot_share_pct` (needs `bot`), `top_rank` (needs `top`) |
| `op` | `>=`, `>`, `<=`, `<`, `==`; `confidence` compares grade order (high > medium > low) |
| `value` | a number — or `low`/`medium`/`high` for `confidence` |
| `id` | a short slug: the rule's address in the verdict |
| `label` | optional wording in the user's terms; printed instead of the metric name |

Where the verdict lands: `analysis.json` → `criteria.verdicts.<lang>.<id>`
(`{value, passed}`) plus `criteria.summary.<lang>` (`{met, total,
not_evaluable}`); the report shows a "Your criteria" block — heading, one line
per rule with each language's mark, then the `n/m` summaries — in **both** the
HTML and the PDF.
`passed: null` *with a `reason`* means the rule could not be measured — a
coverage gap, a layer that was not fetched, an unavailable growth window —
and is never a failure. The manifest refuses a layer-bound rule without its
layer, exactly like `rank_by`.

Workflow: **ask** what success means → write the rules → `run.py all`
(cached responses, no new requests) → quote `criteria.verdicts` in your
answer. Never re-apply the thresholds yourself: the code's verdict is the
one to report. Editing a rule only needs another `all` — the fetch layer is
cached.

Notes: only columns whose metric was measured are printed — KPI tiles and
confidence grades are unchanged. Each layer costs
extra requests (3 / 1 / 1 per language for `access` / `bot` / `top`), so add
them when the question needs them. When you enable a layer, add its header to
the `table_key` block too (`Mobile %`, `Bot %`, `Rank`) — an entry the table
never prints is warned about, and a printed header without one falls back to
the shipped English wording with a visible note.

## Answering the user's question

Read `analysis.json`. Suggested mapping:

* **"Is interest growing?"** → `metrics.<lang>.yoy.article_pct` (absolute) and
  `yoy.share_pct` (normalised). Quote **both**. If their signs differ, say the
  move is traffic-driven, not topic-driven (`flags.direction_agrees` is false).
* **"How confident are we?"** → `metrics.<lang>.confidence` plus
  `confidence_reasons`, which lists exactly what downgraded it. `medium` with
  stated reasons is a normal, honest result for a 24-month window.
* **"Which audience next?"** → rank candidates in this order:
  1. drop any language whose `confidence` you could not defend — a `low` grade
     means the skill's own output does not support a trend claim there;
  2. `comparison.by_growth_share_pct` for momentum;
  3. `comparison.by_share_ppm` for how much that audience cares *today* — use it
     as the counter-case whenever it disagrees with (2).

  **If every language is declining, say so explicitly.**
  `comparison.fastest_growth` means *least bad*; it is never evidence that
  anything is growing. Cross-check `limitations` before recommending anything.
* **"Why is this number odd?"** → `seasonality` (seasonal spikes dominate short
  windows), `trend.share.r2` (how much a straight line explains), `warnings`.
* **"How much of that is real traffic?" / "phones or desktop?"** →
  `metrics.<lang>.bot_share_pct` (lower = cleaner), `access_split.mobile_pct`,
  and `top_rank.rank` (`null` = outside the top `list_size`). These exist only
  when the layer was requested — say so instead of estimating.
* **"Did it pass *their* bar?"** → `criteria.verdicts.<lang>.<id>` and
  `criteria.summary` — quote the verdict (`pl 2/3 met`); do not re-apply the
  thresholds yourself. `passed: null` means *not measurable*, not failed.

Always carry `assumptions` and `limitations` into your answer. Never present a
pageview figure as market size — pageviews are **article reads, not willingness
to pay**.

## Follow-up and refinement

The manifest exists so iteration is cheap. Edit `study.json`, then rerun `all`
with the same `--study`/`--out`:

| Want | Change |
|---|---|
| Another language | `study.json` → `"languages": ["pl", "cs", "uk"]` |
| Longer window | `study.json` → `"window": {"since": "2023-01", "until": "2026-08"}` — **use 23/24 or 47/48 months**, or the halves stop being the same calendar months and confidence is capped at `medium` (`init` warns) |
| Different concept | re-run `init --force --study …`, or pass `--qid Q…` to `resolve`/`all` |
| Report in another language | `run.py all --report-lang <code> …` (or `init --report-lang`); translate the new `translations.<code>.json` |
| Explain the table differently | `study.json` → `"table_key"` block (`"heading"` plus one entry per abbreviated column); printed verbatim as the key under the table |
| Split views by device | `study.json` → `"layers": ["access"]` (adds `Mobile %` and `metrics.<lang>.access_split`) |
| Rank by cleaner traffic, or check the top list | `"layers": ["bot"]` (or `["top"]`) plus `"criteria": {"rank_by": "bot_share"}` — see [Data layers](#data-layers-and-the-users-own-criteria-optional) |
| Grade against the user's own thresholds | `"criteria": {"success": [{"id": …, "metric": …, "op": …, "value": …}]}` — verdict in `analysis.json → criteria` and in the report; rerun `all` (cache, no new requests) |
| Try a substitute | **ask first (step 3)**, then `run.py override --lang … --title … --study …` |
| Force fresh data | `run.py all --no-cache --study … --out …` |

## Rules

1. **Never hand-write a pageviews URL.** Title encoding and URL building live in
   exactly one place, `scripts/api.py` (re-exported as `common.*`). Everything goes through
   `common.per_article_url` / `common.aggregate_url` / `common.action_api`.
2. **Never substitute an article automatically — and never skip one silently.**
   A `GAP` stops the workflow: **ask the user** with the candidates from
   `status` (step 3). Only their answer leads to `override` (recorded and
   disclosed) or to a documented gap.
3. **Never report a title you have not confirmed** with `prop=info`.
4. **Never claim a trend the data does not support.** Quote `confidence` and its
   reasons; state assumptions and limitations in the reply.
5. **The PDF carries everything the HTML does.** Both are rendered from the
   same blocks, so the PDF paginates rather than dropping content; length
   only fails (non-zero exit, HTML kept, PDF not written) when a single block
   cannot fit an empty page.
6. **Stay polite to the API.** Sequential requests, fixed delay, backoff on
   429/5xx, and a `User-Agent` on every call — Wikimedia requires one and the
   skill always sends it (set `WIA_USER_AGENT` to add your contact details).
   The numeric rate limit was never probed.
7. **Write in the user's prompt language** — the report, the chart labels
   **and your answer in the chat** (see [Step 0](#step-0--language-before-anything-else)
   and [Report language](#report-language)). Pass `--report-lang` to `init`;
   English is the answer only for an English prompt. Translate via
   `translations.<lang>.json`, never by editing templates or shipped code, and
   translate it **before** you show the user the report — an English report for
   a non-English prompt is a bug, not a fallback.
   Anything untranslated falls back to English *with a visible note* —
   partial output is admitted, never hidden. Data files and console wording
   stay English. Table headers stay English codes too: their wording lives in
   the manifest's `table_key` block, which you write once, in the report's
   language, and which the report prints as the key under the table.

## When something goes wrong

| Symptom | Cause → fix |
|---|---|
| `study manifest not found` | run `init --study …` first |
| `study has no resolution yet` | run `resolve --study …` first (a real subcommand; `all --stage resolve` works too) |
| `… article '…' does not exist` | typo in `override`; copy the title from `status` |
| `pl: GAP` | genuinely no article → **ask the user** to pick a candidate or confirm the gap (step 3); never choose yourself |
| report/answer in English although the prompt wasn't | `--report-lang` was omitted — set it in `init` (or `"report_language"` in `study.json`), translate `translations.<code>.json`, rerun |
| `N requested month(s) not loaded yet` | trailing months absent from the project series; the window was trimmed and the warning says so |
| `report cannot be printed: '…' needs …` | one block is taller than a whole page (e.g. an enormous headline); shorten it — the HTML was still written. A merely *long* report is fine: the PDF just uses more pages |
| `HTTP 404 … invalid route` | a malformed path (client bug), not missing data |
| `no data for those date(s)` | valid request, nothing in that range; check the window is after 2015-07 |
| `series.json not found -- run 'run.py all --stage fetch' first` | stages are ordered; run the earlier one (or just `all`) |
| `warning: no translations for 'pl' at …` | first run in that language; the English reference was written — translate it and rerun |
| `Warning (pl): Untranslated text: N of M …` | that many report strings had no translation and are in English; fill them in `translations.pl.json` and rerun — a `table_key` entry there means the manifest has no `table_key` block |
| `table_key entries with no matching table column (ignored): …` | that key names a column the table never prints; fix or drop the entry in `study.json` |
| `criteria.rank_by '…' requires the '…' data layer` | the ranking criterion needs data the study does not fetch; add that layer to `"layers"` in `study.json` and rerun |
| `criteria.success.N.metric: Input should be …` | that metric is not on the closed list — the message names the valid ones (and `mobile_pct`/`bot_share_pct`/`top_rank` each need their layer too) |
| `criteria.success.N.value: … 'confidence' rule takes the grade …` | `confidence` compares grades (`low`/`medium`/`high`); every other metric takes a number |
| `criteria.success.N: duplicate id '…'` | two rules share an id — give each its own slug |
| `error: cannot read translations …` | the file is not valid JSON; fix it (a corrupt file stops the run rather than rendering English silently) |
| `<path> already exists (use --force …)` | `i18n-template` will not overwrite a finished translation; add `--force` only to reset it |
| `UnicodeEncodeError` printing a title | fixed by `common.configure_console()`; if you add a new entry point, call it |

## Deeper reading (load only when needed)

* [`references/api.md`](references/api.md) — verified endpoint recipes, path
  parameter enums, failure modes, and an explicit *not verified* list.
* [`references/methods.md`](references/methods.md) — every metric, every
  threshold, and which numbers are design parameters rather than facts.
* [`references/edge-cases.md`](references/edge-cases.md) — traps that silently
  corrupt conclusions (truncated final month, omitted zeros, stale titles,
  coverage gaps) and how each is handled.

## Scope and known limits

* v1 is **monthly** granularity, one concept (Q-item) per study, one article per
  language edition as a proxy for the topic.
* Data starts 2015-07; the current month is always excluded.
* No significance testing — see `references/methods.md` §7 for why.
* Optional data layers (`access`, `bot`, `top`), `criteria.rank_by` and
  `criteria.success` add table columns, a stated ranking order and a graded
  verdict against the user's own thresholds — never new KPI tiles, never a
  different confidence grade; a long report spans more pages instead of
  losing any of them.
* Multi-article topic clusters, daily granularity and larger date ranges are
  planned extensions rather than supported features.
