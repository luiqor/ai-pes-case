# Methods, thresholds and their rationale

Everything the analysis stage computes, why it is computed that way, and which
numbers are **design parameters** rather than facts. The implementation lives in
`scripts/analyze.py`; this file is the reference for anyone questioning a
figure in a report.

## 1. What is measured

| Quantity | Definition |
|---|---|
| `article_views[m]` | Human pageviews of the resolved article in month `m` (`agent=user`, `all-access`) |
| `project_views[m]` | Human pageviews of the *entire* language edition in month `m` |
| `share_ppm[m]` | `article_views[m] / project_views[m] * 1e6` — the article's share of all reading in that edition, per million views |

**Why both?** Language editions differ enormously in size (pl.wikipedia is
roughly 3x cs.wikipedia), so absolute counts are not comparable across
languages. The normalised share answers "how much of this audience cares",
while the absolute count answers "how many readers are there at all". A
language edition that grows 10% overall will show growing absolute views for
nearly every article — that is traffic, not topic interest.

`share_ppm` for a period is computed from **period totals**
(`sum(article) / sum(project) * 1e6`), not as a mean of monthly ratios, so that
months with unusually low project traffic cannot dominate the result.

## 2. Window

* Default: the last **24 complete calendar months**.
* `--since` / `--until` (`YYYY-MM`) override it.
* The current month is always excluded: it is partial by definition.
* The window is clamped forward to `2015-07`, the first month of pageview data.
* A window that ends before `2015-07` after clamping is an error, not zeros.

### The overshoot rule (important)

The window's final month must **not** be the last bucket of a pageviews request.
See `references/edge-cases.md` for the measured evidence: requesting a range that
*ends* at a month returns only that month's first day (2 views instead of 119
for the same article and month). `scripts/fetch.py` therefore requests one
extra month and discards it. `tests/test_fetch.py::test_the_final_month_is_not_truncated`
guards this.

## 3. Growth: second half vs first half

Let `n` be the number of months and `k = n // 2`:

* **first half** = months `[0 .. k-1]`
* **second half** = months `[n-k .. n-1]`
* if `n` is odd, the single middle month is dropped

Dropping the middle month is what keeps the two halves covering **identical
calendar months one year apart**. For 2024-10..2026-08 (23 months) that gives
2024-10..2025-08 versus 2025-10..2026-08 with 2025-09 discarded, so both halves
contain the same seasons.

Growth is then reported three ways:

* `article_pct` — absolute change in views
* `project_pct` — change in the whole wiki (the traffic baseline)
* `share_pct` — change in normalised share

**Interpretation rule:** a trend "holds" only when `article_pct` and
`share_pct` have the same direction, where moves under **1.0 percentage point**
are treated as flat (`FLAT_TOLERANCE_PCT`). If they disagree, the change is
driven by overall wiki traffic and the confidence grade drops to `low`.

**Alignment.** The two halves are compared month for month, which only means
anything when they land on the same calendar months. With `n` months the halves
are offset by `n − ⌊n/2⌋`, so alignment requires that offset to be a whole
number of years:

| Window | Split | Aligned |
|---|---|---|
| **24 months — the default** | 12 + 12, offset 12 | **yes** |
| 23 months (default, one month dropped) | 11 + 11, offset 12 | **yes** |
| 47 / 48 / 71 / 72 months | offset 24 / 36 | **yes** |
| 12 months | 6 + 6, offset 6 | **no** — Jan–Jun vs Jul–Dec |
| 36 months ("the last 3 years") | 18 + 18, offset 18 | **no** |
| 44 months | 22 + 22, offset 22 | **no** |

When `yoy.aligned` is false the halves are not comparable and confidence is
capped at `medium` (`grade()`). This is why the default is 24 rather than 12:
twice the data *and* the only shorter length that splits into two whole calendar
years. Note that **longer is not automatically better** — a 44-month study can
score worse than the 24-month default.

`fetch.resolve_window` warns about this at `init`/`resolve` time so extending a
window cannot silently weaken the study, and
`tests/test_analyze.py::test_alignment_follows_the_offset_rule` pins the rule.

**Limitation:** the two halves are not the same calendar months when the window
is unaligned, so seasonality can masquerade as growth. The odd-middle-month drop
above is what keeps 23-month windows like-for-like.

## 4. Trend: least-squares slope and R²

Ordinary least squares of the monthly series against month index, in plain
Python (no SciPy, no NumPy dependency for this step):

```
slope  = S[(x - x̄)(y - ȳ)] / S[(x - x̄)²]
R²     = 1 - SSE / SST          (0.0 when SST == 0)
```

Reported for both `article` and `share`. `r2` is the fraction of month-to-month
variance a straight line explains — for a strongly seasonal series it is
expected to be poor, which is exactly why it feeds the confidence grade rather
than being presented as "trend strength" on its own.

## 5. Seasonality

`mean(article_views)` grouped by calendar month (1–12), using pandas for the
grouping.

* `peak_ratio` = peak month's mean / overall monthly mean
* `strong` when `peak_ratio >= 2.0`
* `reliable` only when the window spans **≥ 24 months** (two full cycles)

`available` (a factual description of the data) and `reliable` (usable for
grading) are kept separate on purpose. Penalising a confidence grade with an
estimate the same output already labels unreliable would be inconsistent, so an
observed-but-unreliable seasonal peak is reported as a caveat
(`flags.seasonality_observed`) without forcing the grade down by itself.

## 6. Confidence grade

A deterministic rule table, applied in `grade()`. It starts at `high` and
downgrades; every downgrade is written to `confidence_reasons` so the reader can
see precisely why a grade was or was not given.

| Condition | Result |
|---|---|
| window < 12 months | `low` |
| absolute and normalised direction disagree | `low` |
| strongly seasonal **and** share R² < 0.4 | `low` |
| mean < 50 views/month | `low` |
| halves not calendar-aligned | `medium` |
| strongly seasonal (reliable) | `medium` |
| share R² < 0.6 | `medium` |
| window < 24 months | `medium` |
| none of the above | `high` |

**These thresholds (12 / 24 months, R² 0.6 and 0.4, 2.0x, 50 views/month,
1.0pp tolerance) are design parameters, not facts.** They were chosen
conservatively and are exercised by `tests/test_analyze.py`. They are stated as
assumptions in every report's `assumptions` field. If a user has a different
risk tolerance, they should read the underlying numbers rather than the grade.

A grade of `medium` is the normal outcome for the default 24-month window
(under two full seasonal cycles when data completeness trims a month). `high`
requires a complete window with a clean linear fit and no seasonal dominance.

## 7. What is deliberately *not* claimed

* Pageviews measure **article reads, not willingness to pay**. This is a
  direction indicator for further investigation, never a market size.
* One article per language stands in for the topic; how much of the topic each
  edition's article covers can differ.
* Coverage gaps are reported, never filled with a substitute article. When a
  human *does* choose a substitute via `override`, that is disclosed in both the
  headline and the limitations, with the reason.
* No significance testing: monthly counts are small for niche topics and a p-value
  would imply more rigour than a 23-point seasonal series can support.

## 8. Reproducibility

* Dependencies are pinned by `pyproject.toml` + `uv.lock`.
* Responses are cached on disk under `cache/`, so a rerun of an unchanged study
  costs zero requests and returns byte-identical results.
* Requests are sequential with a fixed 0.5 s gap and exponential backoff on
  429/5xx; the numeric rate limit was deliberately never probed.
* Recorded fixtures in `tests/fixtures/` freeze the exact responses behind the
  golden numbers, so `uv run pytest` reproduces every published figure offline.

## 9. Optional data layers and `criteria.rank_by`

Off by default: requested per study in `study.json` (`"layers"`), so their
cost is visible in the manifest and a study without them is byte-identical to
the pre-layer behaviour.

| Quantity | Definition | Notes |
|---|---|---|
| `access_split` | Share of the window's article views per channel: `desktop_pct`, `mobile_web_pct`, `mobile_app_pct`, and `mobile_pct = web + app`; denominator is the base `all-access` total | The three channels sum to `all-access` exactly (verified 2026-09-27), so this is a partition of what the study already measured, not a new estimate |
| `bot_share_pct` | `(all_agents - user) / all_agents * 100` over the window, clamped at 0 | `all-agents = user + spider + automated` exactly (verified), so the difference is precisely non-user traffic; a traffic-quality gauge, not audience size |
| `top_rank` | `{month, rank, list_size}` in the project's monthly top list at the window's last month | `rank: null` = outside the listed top `list_size`, printed as `>N` and never as 0; a month the endpoint has not loaded yet is skipped with a warning |
| `comparison.ranked_by` | `{criterion, order}` — measured languages, best first, for `criteria.rank_by` | `bot_share` ranks **ascending** (cleanest human traffic leads); ties break on the language code; a language with no value for the criterion is left out of the order rather than scored 0 |

Design decisions: a layer adds table columns only — never a KPI tile, never a
different confidence grade, never a second page. The report prints its ranking
reason under the table (`Ranked by …`) so a reordered table explains itself,
and a layer-bound criterion without its layer is refused at the manifest
(`payloads.validate_study`) instead of silently falling back to another order.
