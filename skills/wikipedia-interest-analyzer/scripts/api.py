"""URL building and the two Action API entry points -- the single source of truth.

Everything that knows *what URL to ask Wikimedia for* lives here:

* the REST v1 base and Action API endpoint constants plus their defaults,
* the **only** implementations of title percent-encoding and pageviews /
  Action API / Wikidata URL building,
* the entry points (:func:`action_api`, :func:`wikidata_api`) that issue a
  request through an injected transport (see :class:`errors.JsonFetcher`),
* interpretation of the ``query.pages`` shapes both entry points return.

Other scripts must build URLs through these helpers rather than by hand, so
that title percent-encoding has exactly one implementation. :mod:`common`
re-exports every public name, so callers keep addressing them as
``common.<name>``.

No session, no retries, no cache: the *how* of requesting lives in
:mod:`http_client`, the month math in :mod:`months`, the failure vocabulary
in :mod:`errors`.
"""

from __future__ import annotations

import re
import urllib.parse
from typing import Any

from errors import ApiError, JsonFetcher
from months import month_to_stamp

REST_BASE = "https://wikimedia.org/api/rest_v1/metrics/"
ACTION_API = "https://{host}/w/api.php"
WIKIDATA_API = "https://www.wikidata.org/w/api.php"

DEFAULT_ACCESS = "all-access"
DEFAULT_AGENT = "user"
DEFAULT_GRANULARITY = "monthly"

#: Path-parameter enums for the pageviews API (references/api.md). They live
#: here, with the URL builders, so a path enum has exactly one definition;
#: :mod:`payloads` re-exports them for manifest validation.
ACCESS_VALUES = ("all-access", "desktop", "mobile-app", "mobile-web")
AGENT_VALUES = ("all-agents", "user", "spider", "automated")

_LANG_RE = re.compile(r"^[a-z][a-z0-9-]{0,17}$")


def encode_title(title: str) -> str:
    """Percent-encode an article title for use in a REST path segment.

    Spaces become ``_`` (matching the form the API echoes back), then every
    byte outside the unreserved set is percent-encoded from UTF-8.

    Example: ``Přerušovaný půst`` -> ``P%C5%99eru%C5%A1ovan%C3%BD_p%C5%AFst``
    """
    normalized = title.strip().replace(" ", "_")
    return urllib.parse.quote(normalized, safe="")


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
    """Build the per-article pageviews URL (the single encoder lives here).

    Args:
        project: Host such as ``pl.wikipedia.org`` (see ``project_for``).
        title: Raw article title; percent-encoded by ``encode_title``.
        since: Window start month, ``YYYY-MM`` (inclusive).
        until: Window end month, ``YYYY-MM`` (inclusive).

    Returns:
        A REST v1 ``pageviews/per-article`` URL.

    Raises:
        ValueError: On a malformed month or a ``granularity`` the endpoint
            rejects (hourly returns 404 invalid route -- verified).
    """
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
    """Build the project-wide aggregate pageviews URL.

    Args:
        project: Host such as ``pl.wikipedia.org`` (see ``project_for``).
        since: Window start month, ``YYYY-MM`` (inclusive).
        until: Window end month, ``YYYY-MM`` (inclusive).

    Returns:
        A REST v1 ``pageviews/aggregate`` URL.

    Raises:
        ValueError: On a malformed month.
    """
    return (
        f"{REST_BASE}pageviews/aggregate/{project}/{access}/{agent}/"
        f"{granularity}/{month_to_stamp(since)}/{month_to_stamp(until)}"
    )


def top_url(project: str, access: str, month: str) -> str:
    """Build the project's monthly top-pages URL (whole month, ``all-days``).

    Verified live (2026-09-27): ``items[0].articles`` is a list of
    ``{"rank", "article", "views"}`` entries -- see ``references/api.md``.
    The path carries no agent segment: a top list is one list per
    ``{project}/{access}``, whatever agent filter the rest of the study uses.

    Args:
        project: Host such as ``pl.wikipedia.org`` (see ``project_for``).
        access: One of :data:`ACCESS_VALUES` -- the top list is per channel.
        month: Complete month, ``YYYY-MM``.

    Returns:
        A REST v1 ``pageviews/top`` URL for every day of that month.

    Raises:
        ValueError: On a malformed month or an unknown ``access``.
    """
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
        raise ValueError(f"month must be YYYY-MM: {month!r}")
    if access not in ACCESS_VALUES:
        raise ValueError(f"unknown access {access!r}; expected one of {ACCESS_VALUES}")
    year, mon = month.split("-")
    return f"{REST_BASE}pageviews/top/{project}/{access}/{year}/{mon}/all-days"


def _api_object(url: str, client: JsonFetcher) -> dict[str, Any]:
    """GET ``url`` via ``client`` and require a JSON object body.

    Args:
        url: Fully built Action API URL (query string included).
        client: Injected transport (production: ``http_client.WikiClient``).

    Returns:
        The decoded body, narrowed to a dict.

    Raises:
        ApiError: ``kind="bad_body"`` when the body is not a JSON object
            (an HTML error page served with 200, a proxy page, an API
            change) -- never a bare AttributeError later on ``body.get``.
    """
    body = client.get_json(url)
    if not isinstance(body, dict):
        raise ApiError(
            f"Action API returned a non-object body from {url}",
            url=url,
            kind="bad_body",
            detail=body,
        )
    return body


def action_api(
    host: str, params: dict[str, Any], *, client: JsonFetcher
) -> dict[str, Any]:
    """MediaWiki Action API call (used to confirm titles exist and to search).

    Args:
        host: Wiki host such as ``pl.wikipedia.org``.
        params: Action API query parameters (``format=json`` is added).
        client: Injected transport -- construct one client per run so the
            request cadence is shared by every call.

    Raises:
        ApiError: ``kind="bad_body"`` when the response is not a JSON object.
    """
    query = urllib.parse.urlencode({"format": "json", **params})
    return _api_object(f"{ACTION_API.format(host=host)}?{query}", client)


def wikidata_api(params: dict[str, Any], *, client: JsonFetcher) -> dict[str, Any]:
    """Wikidata Action API call (concept resolution: search + sitelinks).

    Args:
        params: Action API query parameters (``format=json`` is added).
        client: Injected transport (see :func:`action_api`).

    Raises:
        ApiError: ``kind="bad_body"`` when the response is not a JSON object.
    """
    query = urllib.parse.urlencode({"format": "json", **params})
    return _api_object(f"{WIKIDATA_API}?{query}", client)


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
