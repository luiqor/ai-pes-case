# Wikimedia Page view analytics — API investigation notes

Groundwork for the pageview-analysis skill. Everything below marked **verified** was produced by
actually running the request against production (2026-09-23) or by reading the cited document.

Primary docs: <https://doc.wikimedia.org/generated-data-platform/aqs/analytics-api/reference/page-views.html>

## Base URL and conventions (verified)

- Base path: `https://wikimedia.org/api/rest_v1/metrics/`
- Machine-readable spec (Swagger 2.0): `https://wikimedia.org/api/rest_v1/metrics/pageviews/api-spec.json`
  — use this for exact enums/path shapes instead of guessing.
- Machine-readable spec source for the reference page:
  <https://gitlab.wikimedia.org/repos/generated-data-platform/aqs/analytics-api/-/raw/main/reference/page-views.md>
- Data starts **2015-07-01**. Legacy `pagecounts` covers **2008-01 → 2016-07**.
- A `User-Agent` header is **required** on every request (access policy). Clients without one may be
  blocked without notice. Preferred format:
  `<client name>/<version> (<contact information>) <library/framework name>/<version>`
- Response headers observed: `cache-control: s-maxage=14400, max-age=14400` (4 h edge cache),
  `access-control-allow-origin: *`, `access-control-allow-methods: GET,HEAD`.
- Data license: **CC0 1.0**.
- Rate limits: dependent on client identity/access; enforced at the storage layer (uncached requests
  can be throttled with `429`). **No numeric limit was measured — treat any specific number as
  unknown.** Do not hammer the API to find out.

## Endpoints tried — all returned HTTP 200 (verified)

| Endpoint | Working example |
|---|---|
| aggregate (monthly) | `pageviews/aggregate/en.wikipedia.org/all-access/all-agents/monthly/2023010100/2024010100` |
| aggregate (hourly) | `pageviews/aggregate/en.wikipedia.org/all-access/user/hourly/2026092000/2026092100` |
| per-article (daily) | `pageviews/per-article/en.wikipedia.org/all-access/user/Artificial_intelligence/daily/2026090100/2026092100` |
| per-article (encoded title) | `pageviews/per-article/en.wikipedia.org/all-access/user/Caf%C3%A9/daily/...` → `"article":"Café"` |
| top pages (single day) | `pageviews/top/en.wikipedia.org/all-access/2026/09/15` |
| top pages (whole month) | `pageviews/top/en.wikipedia.org/all-access/2023/03/all-days` |
| top-by-country | `pageviews/top-by-country/en.wikipedia.org/all-access/2026/08` |
| top-per-country | `pageviews/top-per-country/FR/all-access/2026/09/15` |
| legacy pagecounts | `legacy/pagecounts/aggregate/en.wikipedia.org/all-sites/monthly/2015010100/2016010100` |
| v3 per_editor | `pageviews/v3/per_editor/12345/monthly/20250101/20260101` |
| v3 top_pages_per_editor | `pageviews/v3/top_pages_per_editor/12345/monthly/20250101/20260101` |

Path parameter enums (from the OpenAPI spec):

- `project` — domain + subdomain, e.g. `en.wikipedia.org`; `all-projects` for the cross-project view.
- `access` — `all-access` | `desktop` | `mobile-app` | `mobile-web`.
- `agent` — `all-agents` | `user` | `spider` | `automated`.
- `granularity` — `hourly` | `daily` | `monthly` (aggregate); `daily` | `monthly` (per-article);
  `daily` | `monthly` (v3 per_editor); `monthly` only (v3 top_pages_per_editor).
- Dates — `YYYYMMDDHH` for v1 endpoints, `YYYYMMDD` for v3 endpoints.
- `access-site` (legacy only) — `all-sites` | `desktop-site` | `mobile-site`.

## Gotchas (verified by observing them)

1. **`top` takes exactly 5 segments**: `{project}/{access}/{year}/{month}/{day}`, where `day` is
   either `DD` **or** the literal `all-days`. Supplying both (`.../09/15/all-days`) fails with a
   router-level `404 {"detail":"invalid route"}`.
2. **Two distinguishable 404 bodies**:
   - `"detail":"invalid route"` → the path itself is malformed (client bug).
   - `"detail":"The date(s) you used are valid, but we either do not have data for those date(s),
     or the project you asked for is not loaded yet…"` → zero views or not-yet-loaded data.
3. **`per-article` rejects `hourly`** with `404 invalid route` (only `daily`/`monthly` are allowed).
4. **Monthly ranges must contain a full month**: `.../monthly/2024010100/2024010100` →
   `400 {"detail":"no full months between dates"}`.
5. **Current-period data is missing until loaded**: `top/.../2026/09/all-days` returned the
   "not loaded yet" 404 while `top/.../2026/09/15` returned 200. Data loads at the end of the time
   span, usually within hours, up to 24 h+ on problems.
6. **Zeros are omitted from time series**, so gaps are normal and must be filled before charting
   (the docs explicitly warn this breaks charting libraries).
7. **Country figures are bucketed and rounded** for privacy: `"views":"100000000-999999999"` plus a
   `views_ceil` upper bound. Only countries with >100 views appear, and `rank` values have gaps.
8. **A page titled `-`** is the API's "no title found" placeholder (search pages, diff pages, etc.)
   and can show implausibly high view counts — filter it out.

## Data semantics (from the concepts doc, not measured)

- A page view = a request returning `200 OK` or `304 Not Modified` with `text/html` (or
  `application/json` for mobile app API requests). Edits, most `Special:` namespace pages, and
  redirects are not counted as views of the target page.
- `spider` = user agents that self-identify as bots; `automated` = traffic flagged by heuristics.

## Endpoint equivalences (verified 2026-09-27)

Measured on production per article over the golden window (2024-10 → 2026-08); these are what the
skill's optional data layers rest on:

- **Access channels partition `all-access`**: `desktop + mobile-web + mobile-app` equals the
  `all-access` series exactly (no rounding, no overlap, month by month). So the device split is a
  partition of what the base study already measured — its percentages are safe to print as-is.
- **`all-agents` decomposes exactly**: `user + spider + automated` equals `all-agents`, so
  `all-agents - user` is precisely the non-human share (no residual to explain away).
- **`top` whole-month body**: `items[0].articles` is a list of `{"rank", "article", "views"}` with a
  1-based `rank` (1000 entries observed). The path has **no agent segment** — one list per
  `{project}/{access}`, whatever agent filter the rest of the study uses. A title absent from the
  list is a result (`rank: null`), not a failure; titles must be matched case-insensitively with
  `_` mapped to a space.

## Not verified / open

- Exact rate-limit numbers (deliberately not probed).
- Whether `all-agents` vs `user` materially changes top-page rankings (the two series were proven
  equal in decomposition, but the *ranking order* effect was never measured).
- Behavior of `429` responses (never triggered).
