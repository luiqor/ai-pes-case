---
name: wikipedia-interest-analyzer
description: Measures how reader interest in a topic changes over time across Wikipedia language editions using Wikimedia pageview data, then renders a comparison chart and a strictly one-page PDF/HTML report with an explicit confidence grade, assumptions and limitations. Use when someone asks which topics or languages to invest in, whether interest in a topic is growing or declining in a given language edition, how confident a trend is, which audience to investigate next, or wants a shareable report comparing audiences — for example "compare interest in intermittent fasting in the Polish and Czech Wikipedias over the past two years".
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
draws a chart and emits a one-page report.

It is deliberately sceptical of its own output. Every report carries a
confidence grade with the reasons behind it, plus its assumptions and
limitations. **A coverage gap is reported as a gap — never quietly replaced by a
similar article.**

## Setup (once)

```bash
cd skills/wikipedia-interest-analyzer
uv sync                      # installs pinned deps from uv.lock
uv run pytest                # optional: 224 offline tests, ~10s
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
uv run python scripts/run.py init --topic "intermittent fasting" --langs pl,cs --study "$WORK/study.json"
```

Optional `--since 2024-10 --until 2026-08` fixes the window (default: the last
24 complete months — `init` prints the resolved dates, not `(default)`). This
writes a small manifest you can edit later instead of re-querying.

Optional `--report-lang pl` sets the report/chart language once for every
rerun (see [Report language](#report-language) — unless the user asks
otherwise, use the language they prompted in).

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
  listed underneath for *you* to judge;
* `window: 2024-10 .. 2026-08` — the dates actually used.

### 3. Fill gaps explicitly (only if you decide to)

```bash
uv run python scripts/run.py override --lang pl --title "Post" --study "$WORK/study.json"
```

This is the **only** way a substitute gets used, and it must be your decision.
It is then disclosed in the headline, the limitations and `analysis.json`
(`resolved_via: override`, `native_gap: true`). Never invent a title — copy it
from `status` output; a plausible misspelling looks like zero interest.

If you prefer a pure gap, skip this step. The report will show
`pl | no article exists | not measurable`.

### 4. Run everything

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
| `report.pdf` | Strictly one page — the shareable artifact |
| `report.html` | Same content, self-contained (chart embedded), no page limit |
| `chart.png` / `chart.svg` | Two panels: absolute views, normalised share |
| `translations.<lang>.json` | Report/chart strings for `--report-lang`; written (English) on first use, translated in place |

### Report language

The report and the chart are written in the language the user prompted in —
unless they ask for another one. Pick the code yourself (`pl`, `cs`, `de`,
`fr`, `pt-br`, … any language the agent can translate into); the skill ships
**no** translation catalogue, you supply the wording.

1. Pass the code: `run.py init … --report-lang pl` (stored in the manifest)
   or per run: `run.py all … --report-lang pl`. Either way the report, the
   PDF and every chart label use it.
2. The first such run finds no translations and **writes
   `<out>/translations.pl.json`** — the English messages keyed by id — then
   renders this run in English with a visible note. Translate every value in
   `"messages"` (keep the ids and the `{placeholder}` names exactly as they
   are). Optionally pre-write it with
   `run.py i18n-template --lang pl --out <path>` (`--force` to overwrite).
3. Rerun with the same flag. Missing ids fall back to English — the report
   then carries a visible `Warning (pl): Untranslated text: …` note naming
   them, and stderr lists every id. **Never fail and never fall back
   silently.**

What stays English, deliberately: `analysis.json` (the data contract your
answer is built from), `series.json`, the console progress/warning wording,
and message ids everywhere. Numbers, article titles, language codes and the
topic concept are never translated. The `Warning (pl):` prefix on the
fallback note is fixed English so a partially-translated report still admits
it in any language.

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
| Try a substitute | `run.py override --lang … --title … --study …` |
| Force fresh data | `run.py all --no-cache --study … --out …` |

## Rules

1. **Never hand-write a pageviews URL.** Title encoding and URL building live in
   exactly one place, `scripts/api.py` (re-exported as `common.*`). Everything goes through
   `common.per_article_url` / `common.aggregate_url` / `common.action_api`.
2. **Never substitute an article automatically.** Report the gap; if the user
   picks a candidate, use `override` so it is recorded and disclosed.
3. **Never report a title you have not confirmed** with `prop=info`.
4. **Never claim a trend the data does not support.** Quote `confidence` and its
   reasons; state assumptions and limitations in the reply.
5. **Reports stay one page.** If the PDF cannot fit, it fails with a non-zero
   exit and writes *only* the HTML — it will not emit a two-page PDF.
6. **Stay polite to the API.** Sequential requests, fixed delay, backoff on
   429/5xx, and a `User-Agent` on every call — Wikimedia requires one and the
   skill always sends it (set `WIA_USER_AGENT` to add your contact details).
   The numeric rate limit was never probed.
7. **Write the report in the user's language** (see
   [Report language](#report-language)); translate via
   `translations.<lang>.json`, never by editing templates or shipped code.
   Anything untranslated falls back to English *with a visible note* —
   partial output is admitted, never hidden. Data files and console wording
   stay English.

## When something goes wrong

| Symptom | Cause → fix |
|---|---|
| `study manifest not found` | run `init --study …` first |
| `study has no resolution yet` | run `resolve --study …` first (a real subcommand; `all --stage resolve` works too) |
| `… article '…' does not exist` | typo in `override`; copy the title from `status` |
| `pl: GAP` | genuinely no article; pick a candidate or leave it as a gap |
| `N requested month(s) not loaded yet` | trailing months absent from the project series; the window was trimmed and the warning says so |
| `report does not fit on one page` | too much text/languages; split the study or shorten the verdict — HTML was still written |
| `HTTP 404 … invalid route` | a malformed path (client bug), not missing data |
| `no data for those date(s)` | valid request, nothing in that range; check the window is after 2015-07 |
| `series.json not found -- run 'run.py all --stage fetch' first` | stages are ordered; run the earlier one (or just `all`) |
| `warning: no translations for 'pl' at …` | first run in that language; the English reference was written — translate it and rerun |
| `Warning (pl): Untranslated text: N of M …` | that many report strings had no translation and are in English; fill them in `translations.pl.json` and rerun |
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
* Multi-article topic clusters, daily granularity and larger date ranges are
  planned extensions rather than supported features.
