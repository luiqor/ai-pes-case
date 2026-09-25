# Edge cases and failure modes

Every item here was **observed against the live API** (or is a bug found while
building this skill). Each row says what happens, how the code responds, and
where it is enforced. Endpoint mechanics live in `references/api.md`; this file
is about things that can silently corrupt a conclusion.

---

## 1. The last bucket of a per-article range is truncated ⚠️ highest impact

**Observed.** Requesting a monthly per-article range that *ends* on a month
returns only that month's **first day**. Repeated 3/3 times, same minute, no
cache involved:

| Request | `2026-08` value |
|---|---|
| `.../monthly/2024100100/2026080100` | **2** |
| `.../monthly/2024100100/2026090100` | **119** (complete month) |

The aggregate endpoint does **not** show this (`51,467,770` either way), so the
two endpoints disagree about what the end bound means.

**Consequence.** A report whose window ends exactly at its last month silently
loses that month's data — enough to move a growth figure by several percentage
points. It also means a "views in the final month" figure is really "views on
the 1st".

**Handling.** `scripts/fetch.py` requests `until + 1 month` for *both* series
and keeps only months inside the requested window, so the window's last month is
never a range's final bucket.
Guarded by `tests/test_fetch.py::test_the_final_month_is_not_truncated`
(asserting the Czech article's August 2026 = 119, not 2).

## 2. Two entirely different 404 bodies

Both are HTTP 404 with JSON `detail`, but they mean opposite things:

| Body contains | Meaning | Handling |
|---|---|---|
| `invalid route` | the path itself is malformed (a client bug: bad enum, hourly on per-article, wrong number of segments) | raise immediately, no retry — retrying a malformed URL cannot help |
| `do not have data for those date(s)` / `not loaded yet` | valid request, no data in that range | per-article → treat as **all zeros** with a warning; aggregate → hard error (a whole wiki is never zero) |

Misclassifying the first as the second would turn a coding bug into a fake
"nobody is interested" result. See `common._classify`, tested in
`tests/test_common.py`.

## 3. Zero views are *omitted*, not returned as `0`

A monthly series simply has no entry for a month with zero views. Leaving the
gap unfilled produces a chart with holes and a mean computed over too few points.

**Handling.** The window's month list is built first, then values are read with
`.get(month, 0)`.

## 4. The current month is partial — and the previous month may still be loading

* The current month is always excluded (partial by definition).
* The previous month may not be loaded yet; Wikimedia's own documentation says
  data appears "usually within hours, up to 24 h+ on problems".
* The per-article endpoint will happily return a *partial* current month while
  the aggregate endpoint omits it entirely — so the two series can disagree
  about which months exist.

**Handling.** Availability is taken from the **project** series: a loaded month
always has project-wide views, whereas an article can legitimately have zero.
Any requested month absent from the project series is dropped and reported as
`warning: N requested month(s) not loaded yet`.

## 5. A title that looks right can still not exist

**Observed.** Two strings that render identically-ish:

* `Głodówka lecznicza` → exists, `pageid=74017`
* `Głódówka lecznicza` → `missing`

The first is what search returns; the second was a hand-typed transcription.
`prop=info` reported `missing` for it, and an earlier investigation of this
project wrongly concluded the search index was stale.

**Handling.**

* **Never hand-write an article title.** Always copy it from API output
  (`resolution`, `status`, or `candidates`).
* Every title is confirmed with `prop=info` *before* any pageview request. A bad
  `override` fails loudly with *"does not exist … check the spelling"* rather
  than reporting zero interest.
* Guarded by `tests/test_fetch.py::test_misspelled_override_fails…`

## 6. Sitelinks and search hits can be stale

A Wikidata sitelink or a search hit may point at a page that no longer exists.

**Handling.** Both are confirmed with `prop=info`; misses are dropped, the
sitelink case is downgraded to a coverage gap with a warning, and the search case
emits `search result … no longer exists (stale search index) -- dropped`.
See `tests/test_resolve.py::test_stale_search_hits_are_dropped_with_a_warning`.

## 7. `query.pages` has been observed in two shapes

Sometimes an object keyed by pageid (`{"123": {...}}`), sometimes a list
(`[{...}]`). Missing pages use negative keys (`"-1"`, `"-2"`).

**Handling.** `common.iter_pages` normalises both; `common.page_exists` matches
by title case-insensitively with `_` treated as a space (the API echoes titles
with underscores), and treats the presence of a `missing` key as non-existent.

## 8. Coverage gaps must not be papered over

Polish Wikipedia has no article for *intermittent fasting* (the topic resolves to
Q1666254, which has a Czech sitelink but no Polish one).

**Handling, in order of preference:**

1. Report the gap and offer **candidate** articles from that edition's own search,
   already filtered to pages that exist. Candidates are data, not decisions.
2. If a human picks one, they must say so explicitly:
   `run.py override --lang pl --title "Post"`.
3. The substitution is then disclosed everywhere it matters — the headline
   (*"Substitute in use: pl uses 'Post' (no native article exists)"*), a
   limitations bullet explaining it is not like-for-like, and the
   `resolved_via` / `native_gap` fields in `analysis.json`.

Nothing in the skill ever picks a proxy article on its own.
Guarded by `tests/test_run.py::test_pipeline_reports_a_gap_end_to_end`.

## 9. `query` parameters need full months

Monthly granularity rejects a range containing no complete month
(`400 … no full months between dates`). A zero-width range is therefore always
an error, never an empty result.

## 10. Monthly windows vs. legacy docs

The published reference says the end date is *exclusive* for monthly ranges;
observed behaviour on the current API is **inclusive** (a request ending
`2026090100` returns the `2026090100` bucket). Code follows observed behaviour;
this discrepancy is recorded in `references/api.md` rather than silently
assumed either way.

## 11. The numeric rate limit was never measured

Per the access policy the limit depends on client identity and is enforced at
the storage layer (uncached requests can be throttled with `429`).

**Handling.** Never probed. Requests are sequential with a fixed 0.5 s gap,
exponential backoff (1s/2s/4s) on 429 and 5xx only, and a `User-Agent` is set on
every request. Disk caching means iterative follow-up queries usually make zero
requests at all.

## 12. A `User-Agent` is required on every request

Clients without one may be blocked without notice. The skill sends
`wikipedia-interest-analyzer/0.1 (…)` and reads `WIA_USER_AGENT` if you want to
put your own contact details in it — **set it if you will run this regularly.**

## 13. Console encoding on Windows

Article titles are routinely non-ASCII (`Přerušovaný půst`), while the Windows
console may default to `cp1252`, which raises `UnicodeEncodeError` while simply
*printing* a title. This actually crashed the first end-to-end run.

**Handling.** `common.configure_console()` forces UTF-8 with `errors="replace"`
at the start of every CLI entry point. JSON outputs were already written as
UTF-8.

## 14. Fonts must not come from the host

Czech and Polish glyphs (`ř š ů ł ę`) are absent from the PDF base-14 fonts, so
relying on system fonts would produce tofu or depend on the machine.

**Handling.** The PDF registers DejaVu Sans from inside the pinned matplotlib
wheel — reproducible on any platform, no host font dependency. Falls back to
Helvetica with an explicit warning if that ever fails.

## 15. Data availability floor

Pageview data starts **2015-07-01**. Earlier requests are clamped forward with a
warning; a window entirely before that is an error. Legacy `pagecounts`
(2008-01 → 2016-07) is a different dataset and is **not** used — mixing the two
would fabricate a discontinuity.

---

## Known limitations (not bugs, but honest gaps)

* **Not verified:** the `top` and `top-by-country` endpoint shapes beyond the
  documented 5-segment path; titles containing `/` or `#`; daily granularity
  end-bucket behaviour (v1 is monthly only); whether the truncation in (1)
  affects ranges ending on a *daily* boundary. Full list in `references/api.md`.
* **Single concept per query.** One Q-item per study. Multi-article topic
  clusters (e.g. counting several articles that all cover the topic) are an
  intentional future extension.
* **One article per language** is a proxy for the topic, and different editions
  cover different scopes.
* **No significance testing**, for the reasons in `references/methods.md` §7.
