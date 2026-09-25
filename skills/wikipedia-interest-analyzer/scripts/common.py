"""Shared HTTP, caching, and URL-building helpers for wikipedia-interest-analyzer.

Everything that knows how to talk to Wikimedia lives here:

* the **required** ``User-Agent`` header (access policy),
* a polite request cadence and retry/backoff (the rate limit was never probed,
  so this code deliberately stays far away from it),
* the on-disk response cache (reused across runs so follow-up queries are cheap),
* the **only** implementations of pageviews / Action API / Wikidata URL building.

Other scripts must build URLs through these helpers rather than by hand, so
that title percent-encoding has exactly one implementation.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
import urllib.parse
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import requests

SKILL_ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = Path(os.environ.get("WIA_CACHE_DIR") or (SKILL_ROOT / "cache"))

REST_BASE = "https://wikimedia.org/api/rest_v1/metrics/"
ACTION_API = "https://{host}/w/api.php"
WIKIDATA_API = "https://www.wikidata.org/w/api.php"

DEFAULT_ACCESS = "all-access"
DEFAULT_AGENT = "user"
DEFAULT_GRANULARITY = "monthly"

# Verified from the API reference: pageview data starts 2015-07-01.
DATA_START_MONTH = "2015-07"

# The numeric rate limit was deliberately never measured. Stay conservative:
# one request at a time with a fixed gap, and back off hard on 429/5xx.
REQUEST_DELAY_SECONDS = 0.5
MAX_ATTEMPTS = 4
BACKOFF_SECONDS = (1.0, 2.0, 4.0)

USER_AGENT = os.environ.get(
    "WIA_USER_AGENT",
    "wikipedia-interest-analyzer/0.1 (set WIA_USER_AGENT to your contact)",
)

_MONTH_RE = re.compile(r"^(\d{4})-(\d{2})$")
_LANG_RE = re.compile(r"^[a-z][a-z0-9-]{0,17}$")
_TAG_RE = re.compile(r"<[^>]+>")

_cache_enabled = True
_session: requests.Session | None = None
_last_request_at = 0.0


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------
class ApiError(RuntimeError):
    """A failed Wikimedia request, classified so callers can react precisely.

    ``kind`` is one of:
      * ``invalid_route`` -- the path itself was malformed (a client bug).
      * ``no_data``       -- dates valid, but no data loaded for that range.
      * ``rate_limited``  -- HTTP 429.
      * ``server``        -- HTTP 5xx.
      * ``not_found``     -- HTTP 404 that is neither of the two known bodies.
      * ``network`` / ``bad_body`` -- transport or decoding failure.
      * ``other``         -- anything else.
    """

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        url: str | None = None,
        kind: str = "other",
        detail: Any = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.url = url
        self.kind = kind
        self.detail = detail


def _classify(status: int, detail: Any) -> str:
    text = str(detail or "")
    if status == 404:
        # Two distinct 404 bodies were observed; they mean very different things.
        if "invalid route" in text:
            return "invalid_route"
        if "do not have data for those date(s)" in text or "not loaded yet" in text:
            return "no_data"
        return "not_found"
    if status == 429:
        return "rate_limited"
    if status >= 500:
        return "server"
    return "other"


def _safe_detail(response: requests.Response) -> Any:
    try:
        payload = response.json()
    except ValueError:
        return response.text[:500]
    if isinstance(payload, dict):
        return payload.get("detail", payload)
    return payload


# --------------------------------------------------------------------------
# Cache
# --------------------------------------------------------------------------
def set_cache_enabled(enabled: bool) -> None:
    global _cache_enabled
    _cache_enabled = bool(enabled)


def _cache_path(url: str) -> Path:
    return CACHE_DIR / f"{hashlib.sha256(url.encode('utf-8')).hexdigest()}.json"


def clear_cache() -> int:
    """Delete every cached response. Returns the number of files removed."""
    if not CACHE_DIR.is_dir():
        return 0
    removed = 0
    for path in CACHE_DIR.glob("*.json"):
        path.unlink(missing_ok=True)
        removed += 1
    return removed


def _write_cache(url: str, body: Any) -> None:
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        record = {"url": url, "fetched_at": date.today().isoformat(), "body": body}
        _cache_path(url).write_text(
            json.dumps(record, ensure_ascii=False), encoding="utf-8"
        )
    except OSError:
        # A read-only or full cache must never break the analysis.
        pass


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------
def _get_session() -> requests.Session:
    global _session
    if _session is None:
        session = requests.Session()
        session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})
        _session = session
    return _session


def _polite_delay() -> None:
    if not _last_request_at:
        return
    elapsed = time.monotonic() - _last_request_at
    if elapsed < REQUEST_DELAY_SECONDS:
        time.sleep(REQUEST_DELAY_SECONDS - elapsed)


def _backoff(attempt: int) -> None:
    time.sleep(BACKOFF_SECONDS[min(attempt, len(BACKOFF_SECONDS) - 1)])


def get_json(url: str, *, use_cache: bool | None = None) -> Any:
    """GET ``url`` and return the decoded JSON body.

    Cached responses are returned without touching the network or the request
    cadence, which is what makes iterative follow-up queries cheap.
    """
    global _last_request_at

    cached = _cache_enabled if use_cache is None else use_cache
    if cached:
        path = _cache_path(url)
        if path.is_file():
            try:
                return json.loads(path.read_text(encoding="utf-8"))["body"]
            except (json.JSONDecodeError, KeyError, OSError):
                path.unlink(missing_ok=True)  # corrupt entry -> refetch

    last_error: ApiError | None = None
    for attempt in range(MAX_ATTEMPTS):
        _polite_delay()
        try:
            response = _get_session().get(url, timeout=30)
        except requests.RequestException as exc:
            last_error = ApiError(
                f"network error requesting {url}: {exc}", url=url, kind="network"
            )
            _backoff(attempt)
            continue
        finally:
            _last_request_at = time.monotonic()

        if response.status_code == 200:
            try:
                body = response.json()
            except ValueError as exc:
                raise ApiError(
                    f"non-JSON response from {url}: {response.text[:200]}",
                    status=200,
                    url=url,
                    kind="bad_body",
                ) from exc
            if cached:
                _write_cache(url, body)
            return body

        detail = _safe_detail(response)
        kind = _classify(response.status_code, detail)
        error = ApiError(
            f"HTTP {response.status_code} from {url}: {detail}",
            status=response.status_code,
            url=url,
            kind=kind,
            detail=detail,
        )
        if response.status_code == 429 or response.status_code >= 500:
            last_error = error
            _backoff(attempt)  # transient -> retry with backoff
            continue
        raise error  # deterministic 4xx -> retrying would not help

    raise last_error or ApiError(f"request failed after {MAX_ATTEMPTS} attempts: {url}")


# --------------------------------------------------------------------------
# URL construction -- the single implementation of title encoding
# --------------------------------------------------------------------------
def encode_title(title: str) -> str:
    """Percent-encode an article title for use in a REST path segment.

    Spaces become ``_`` (matching the form the API echoes back), then every
    byte outside the unreserved set is percent-encoded from UTF-8.

    Example: ``Přerušovaný půst`` -> ``P%C5%99eru%C5%A1ovan%C3%BD_p%C5%AFst``
    """
    normalized = title.strip().replace(" ", "_")
    return urllib.parse.quote(normalized, safe="")


def month_to_stamp(month: str) -> str:
    """``2024-10`` -> ``2024100100`` (the v1 ``YYYYMMDDHH`` parameter)."""
    match = _MONTH_RE.match(month)
    if not match:
        raise ValueError(f"month must be 'YYYY-MM', got {month!r}")
    return f"{match.group(1)}{match.group(2)}0100"


def project_for(language: str) -> str:
    """``pl`` -> ``pl.wikipedia.org``."""
    if not _LANG_RE.match(language):
        raise ValueError(f"language code looks invalid: {language!r}")
    return f"{language}.wikipedia.org"


def per_article_url(
    project: str,
    title: str,
    since: str,
    until: str,
    *,
    granularity: str = DEFAULT_GRANULARITY,
    access: str = DEFAULT_ACCESS,
    agent: str = DEFAULT_AGENT,
) -> str:
    if granularity not in ("daily", "monthly"):
        # Verified: the per-article endpoint rejects hourly with 404 invalid route.
        raise ValueError("per-article granularity must be 'daily' or 'monthly'")
    return (
        f"{REST_BASE}pageviews/per-article/{project}/{access}/{agent}/"
        f"{encode_title(title)}/{granularity}/{month_to_stamp(since)}/{month_to_stamp(until)}"
    )


def aggregate_url(
    project: str,
    since: str,
    until: str,
    *,
    granularity: str = DEFAULT_GRANULARITY,
    access: str = DEFAULT_ACCESS,
    agent: str = DEFAULT_AGENT,
) -> str:
    return (
        f"{REST_BASE}pageviews/aggregate/{project}/{access}/{agent}/"
        f"{granularity}/{month_to_stamp(since)}/{month_to_stamp(until)}"
    )


def action_api(host: str, params: dict[str, Any]) -> dict[str, Any]:
    """MediaWiki Action API call (used to confirm titles exist and to search)."""
    query = urllib.parse.urlencode({"format": "json", **params})
    return get_json(f"{ACTION_API.format(host=host)}?{query}")


def wikidata_api(params: dict[str, Any]) -> dict[str, Any]:
    """Wikidata Action API call (concept resolution: search + sitelinks)."""
    query = urllib.parse.urlencode({"format": "json", **params})
    return get_json(f"{WIKIDATA_API}?{query}")


def iter_pages(query: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalise ``query.pages`` from either an object keyed by pageid or a list.

    Both shapes were observed live from the Wikimedia Action API, so handle
    both instead of assuming one.
    """
    pages = (query.get("query") or {}).get("pages", {})
    if isinstance(pages, dict):
        return [page for page in pages.values() if isinstance(page, dict)]
    if isinstance(pages, list):
        return [page for page in pages if isinstance(page, dict)]
    return []


def page_exists(query: dict[str, Any], title: str) -> tuple[bool, int | None]:
    """Does ``title`` actually exist? Returns ``(exists, pageid)``.

    Search results and Wikidata sitelinks can both be stale, so any title that
    matters must be confirmed with ``prop=info`` before it is trusted.
    """
    wanted = title.strip().replace("_", " ").casefold()
    for page in iter_pages(query):
        candidate = str(page.get("title", "")).replace("_", " ")
        if candidate.casefold() == wanted and "missing" not in page:
            pageid = page.get("pageid")
            return True, (int(pageid) if pageid is not None else None)
    return False, None


# --------------------------------------------------------------------------
# Month arithmetic
# --------------------------------------------------------------------------
def _validate_month(month: str, label: str) -> None:
    match = _MONTH_RE.match(month)
    if not match:
        raise ValueError(f"{label} must be 'YYYY-MM', got {month!r}")
    if not 1 <= int(match.group(2)) <= 12:
        raise ValueError(f"{label} has an invalid month number: {month!r}")


def month_range(since: str, until: str) -> list[str]:
    """Inclusive list of ``YYYY-MM`` strings from ``since`` to ``until``."""
    _validate_month(since, "since")
    _validate_month(until, "until")
    if since > until:
        raise ValueError(f"since ({since}) is after until ({until})")
    year, month = int(since[:4]), int(since[5:7])
    end_year, end_month = int(until[:4]), int(until[5:7])
    out: list[str] = []
    while (year, month) <= (end_year, end_month):
        out.append(f"{year:04d}-{month:02d}")
        month += 1
        if month == 13:
            month, year = 1, year + 1
    return out


def shift_month(month: str, delta: int) -> str:
    _validate_month(month, "month")
    year, mon = int(month[:4]), int(month[5:7])
    index = year * 12 + (mon - 1) + delta
    return f"{index // 12:04d}-{index % 12 + 1:02d}"


def last_complete_month(today: date | None = None) -> str:
    """The most recent month that has fully elapsed.

    The current month is always partial, so it is never a valid window end.
    Note that Wikimedia may still be loading the previous month (verified:
    "usually within hours, up to 24 h+ on problems"), so callers must also
    tolerate months being absent from a response.
    """
    current = today or date.today()
    return shift_month(f"{current.year:04d}-{current.month:02d}", -1)


def default_window(months: int = 24, today: date | None = None) -> tuple[str, str]:
    """``(since, until)`` covering ``months`` complete months ending last month."""
    if months < 2:
        raise ValueError("window must be at least 2 months")
    until = last_complete_month(today)
    return shift_month(until, -(months - 1)), until


def clamp_to_data_start(since: str) -> str:
    return max(since, DATA_START_MONTH)


# --------------------------------------------------------------------------
# Small utilities
# --------------------------------------------------------------------------
def clip(text: str, limit: int = 80) -> str:
    """Shorten ``text`` to about ``limit`` characters, cutting on a word boundary.

    ``text[:80]`` sliced search descriptions mid-word (``... Diese Pro)``), which
    reads like corruption rather than truncation. Falls back to a hard cut when
    there is no space to break on.
    """
    if len(text) <= limit:
        return text
    cut = text.rfind(" ", 0, limit)
    if cut <= 0:
        cut = limit
    return text[:cut].rstrip(" ,;:.") + "..."


def strip_html(text: str) -> str:
    return re.sub(r"\s+", " ", _TAG_RE.sub("", text or "")).strip()


def configure_console() -> None:
    """Emit UTF-8 on stdout/stderr regardless of the host's locale.

    Article titles are routinely non-ASCII (``Přerušovaný půst``,
    ``Wielki post``), and the Windows console defaults to a legacy code page
    such as cp1252, which raises ``UnicodeEncodeError`` while printing them.
    Call this at the start of every CLI entry point.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):  # pragma: no cover - stream already closed
            pass


def read_json(path: str | Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path: str | Path, payload: Any) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
