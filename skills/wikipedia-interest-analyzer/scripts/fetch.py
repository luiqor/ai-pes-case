"""Fetch pageview series for every language in a study manifest.

Reads ``study.json`` (written by ``run.py init`` / ``resolve``) and writes
``series.json`` with two aligned monthly series per language:

* ``article_views``  -- human views of the resolved article (``agent=user``)
* ``project_views``  -- total views of the whole wiki, used to normalise for
  the very different sizes of language editions (pl.wikipedia is ~3x cs.wikipedia)

Verified behaviours this script encodes:

* zeros are **omitted** from time series, so absent article months become 0;
* the current month is partial and is never requested;
* the previous month may still be loading, so the window is trimmed to the
  months the *project* series actually contains (a whole project is never
  zero views, which makes its presence a reliable "month loaded" signal);
* a ``no_data`` 404 on the article series means "zero views in this window",
  while the same 404 on the project series is a real error;
* every title is confirmed with ``prop=info`` first, so a typo in an override
  can never be silently reported as "no interest".
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path
from typing import Any

import common
import http_client
import resolve as resolve_mod
from payloads import (
    LanguageSeries,
    PageviewPoint,
    RequestParameters,
    Resolution,
    SeriesPayload,
    StudyManifest,
    StudyWindow,
    load_study_manifest,
)
from pydantic import ValidationError

DEFAULT_MONTHS = 24

# Extra month requested beyond the window and then discarded, so that the
# window's final month is never the (truncated) last bucket of a range.
FETCH_OVERSHOOT_MONTHS = 1


def stamp_to_month(stamp: str) -> str:
    """``2024100100`` -> ``2024-10``."""
    return f"{stamp[0:4]}-{stamp[4:6]}"


def load_study(path: Path) -> StudyManifest:
    """Load and validate the study manifest.

    All file handling and schema checks live in
    :func:`payloads.load_study_manifest` (shared with ``run.py``), so the
    standalone script and the orchestrator accept exactly the same manifests
    and print exactly the same errors.

    Args:
        path: Location of ``study.json``.

    Returns:
        The validated manifest.

    Raises:
        SystemExit: The manifest is missing (with the ``run.py init``
            remedy), unreadable, not JSON, or fails validation -- the message
            names every bad field (month patterns, language codes, the
            access/agent/granularity enums).
    """
    return load_study_manifest(path)


def _warn_if_halves_not_aligned(since: str, until: str, warnings: list[str]) -> None:
    """Warn when this window length cannot be split into comparable halves.

    Growth is measured by comparing the second half of the window against the
    first, month for month. That is only a like-for-like comparison when the
    offset between the halves (n - n // 2 months) is a whole number of years --
    so 23, 24, 47, 48, 71, 72... line up, while 12, 35, 36 and 44 do not.
    Extending a study is intuitive; silently capping its confidence at `medium`
    is not, so say it up front.
    """
    months = len(common.month_range(since, until))
    offset = months - months // 2
    if offset % 12 == 0:
        return
    warnings.append(
        f"window of {months} months compares non-identical calendar months, so "
        "confidence is capped at medium; use 23 or 24 months (47 or 48 for a "
        "longer study) for a like-for-like year-over-year comparison"
    )


def resolve_window(
    study: StudyManifest, today: date | None = None
) -> tuple[str, str, list[str]]:
    """Return ``(since, until)`` clamped to complete, available data.

    Clamps, in order: an unset end becomes the last complete month, a future
    end is pulled back to it, a start before 2015-07 is floored, and a window
    with no complete months left is rejected.

    Args:
        study: Manifest whose ``window`` (if any) is being resolved.
        today: Reference date; defaults to ``date.today()`` (tests inject a
            fixed date so the golden numbers stay reproducible).

    Returns:
        ``(since, until, warnings)`` -- both months ``YYYY-MM``, plus every
        adjustment or alignment caveat the caller must disclose.

    Raises:
        SystemExit: If the requested window contains no complete months.
    """
    warnings: list[str] = []
    window: StudyWindow = study.get("window") or StudyWindow()
    since = window.get("since")
    until = window.get("until")

    if not since and not until:
        since, until = common.default_window(DEFAULT_MONTHS, today)
    elif not since:
        until = str(until)
        since = common.shift_month(until, -(DEFAULT_MONTHS - 1))
    elif not until:
        since = str(since)
        until = common.last_complete_month(today)
    else:
        since, until = str(since), str(until)

    latest = common.last_complete_month(today)
    if until > latest:
        warnings.append(
            f"window end {until} is not a complete month yet; clamped to {latest}"
        )
        until = latest

    clamped = common.clamp_to_data_start(since)
    if clamped != since:
        warnings.append(
            f"pageview data starts {common.DATA_START_MONTH}; window start "
            f"{since} clamped to {clamped}"
        )
        since = clamped

    if since > until:
        raise SystemExit(
            f"error: requested window {since}..{until} contains no complete months"
        )
    _warn_if_halves_not_aligned(since, until, warnings)
    return since, until, warnings


def effective_titles(
    study: StudyManifest,
) -> tuple[dict[str, str], dict[str, str], list[str], list[str], list[str]]:
    """Pick one article title per language.

    Returns ``(titles, sources, gaps, native_missing, warnings)``. An explicit
    ``overrides`` entry wins over the resolved sitelink -- substitutions are
    only ever made deliberately by a human, never automatically. ``native_missing``
    lists override languages that have no article of their own for the topic, so
    the coverage gap stays visible downstream.

    Args:
        study: Manifest carrying ``languages``, ``overrides`` and the
            ``resolution`` written by the resolve stage.

    Returns:
        The five-tuple described above; ``sources`` maps language to
        ``"override"`` or ``"sitelink"``.
    """
    overrides: dict[str, str] = study.get("overrides") or {}
    resolution: Resolution = study.get("resolution") or Resolution()
    articles: dict[str, dict[str, Any] | None] = resolution.get("articles") or {}

    titles: dict[str, str] = {}
    sources: dict[str, str] = {}
    native_missing: list[str] = []
    gaps: list[str] = []
    warnings: list[str] = []

    for language in study["languages"]:
        override = overrides.get(language)
        article = articles.get(language)
        if override:
            titles[language] = str(override)
            sources[language] = "override"
            if not article:
                # The substitution exists because no native article does. Keep
                # that fact attached to the data -- an override must never
                # silently erase a coverage gap.
                native_missing.append(language)
        elif article:
            titles[language] = str(article.get("title"))
            sources[language] = "sitelink"
        else:
            gaps.append(language)
            warnings.append(
                f"{language}: no article for this topic -- reporting a coverage gap "
                "(choose a candidate and set overrides.{language} to analyse it)"
            )
    return titles, sources, gaps, native_missing, warnings


def confirm_titles(titles: dict[str, str], *, client: common.JsonFetcher) -> None:
    """Fail loudly if a chosen title does not exist (protects overrides).

    Args:
        titles: Language code to chosen article title.
        client: Injected transport shared with the caller's other requests.

    Raises:
        SystemExit: Naming the offending language and title, with the remedy
            (fix ``overrides.<lang>`` in the manifest).
    """
    for language, title in titles.items():
        info = resolve_mod.confirm_titles(language, [title], client=client)
        exists, _ = common.page_exists(info, title)
        if not exists:
            raise SystemExit(
                f"error: {language} article {title!r} does not exist. "
                "Fix overrides/{} in the study manifest (check the spelling, "
                "or pick a candidate from the resolution output).".format(language)
            )


def _parse_points(payload: Any, url: str) -> dict[str, int]:
    """Parse a pageviews response body into ``{month: views}`` (validated).

    Both endpoints share this shape, so validating the points here turns a
    corrupt or unexpected body into a classified API error instead of a
    ``KeyError``/``ValueError`` traceback in the middle of a fetch.

    Args:
        payload: Decoded JSON body as returned by the injected client.
        url: Request URL, quoted in the error so it can be reproduced.

    Returns:
        Mapping of ``YYYY-MM`` month to view count.

    Raises:
        ApiError: ``kind="bad_body"`` when the body has no ``items`` list or
            contains a point that is not a non-negative monthly bucket.
    """
    items = payload.get("items") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        raise common.ApiError(
            f"unexpected pageviews response (no 'items' list) from {url}",
            url=url,
            kind="bad_body",
            detail=payload,
        )
    try:
        points = [PageviewPoint.model_validate(item) for item in items]
    except ValidationError as exc:
        raise common.ApiError(
            f"malformed pageviews item from {url}: {exc.error_count()} problem(s)",
            url=url,
            kind="bad_body",
            detail=str(exc),
        ) from exc
    return {stamp_to_month(point.timestamp): point.views for point in points}


def _fetch_project_series(
    project: str,
    since: str,
    until: str,
    *,
    access: str,
    agent: str,
    client: common.JsonFetcher,
) -> dict[str, int]:
    """Project-wide monthly views; ``{month: views}``.

    Raises:
        SystemExit: Because a project with no data means the *request* is
            wrong (bad project or pre-2015 window), never "no interest" --
            and a malformed response body fails here the same way
            (``error [bad_body]``), never as a traceback.
    """
    url = common.aggregate_url(project, since, until, access=access, agent=agent)
    try:
        payload = client.get_json(url)
        return _parse_points(payload, url)
    except common.ApiError as exc:
        if exc.kind == "no_data":
            raise SystemExit(
                f"error: {project} reported no data for {since}..{until}. "
                "Check the project name and that the window is after "
                f"{common.DATA_START_MONTH}."
            ) from exc
        raise SystemExit(f"error [{exc.kind}]: {exc}") from exc


def _fetch_article_series(
    project: str,
    title: str,
    since: str,
    until: str,
    *,
    access: str,
    agent: str,
    warnings: list[str],
    client: common.JsonFetcher,
) -> dict[str, int]:
    """Article monthly views; ``{month: views}``.

    A ``no_data`` 404 means genuinely zero views in this window (zeros are
    omitted from the series), so it returns an empty mapping *and* records
    the fact in ``warnings`` rather than failing.

    Raises:
        SystemExit: On a malformed route (title-encoding bug) or any other
            real error -- never for "no views".
    """
    url = common.per_article_url(
        project, title, since, until, access=access, agent=agent
    )
    try:
        payload = client.get_json(url)
        return _parse_points(payload, url)
    except common.ApiError as exc:
        if exc.kind == "no_data":
            warnings.append(
                f"{project}/{title}: no recorded views in {since}..{until} "
                "(treated as zeros -- this means genuinely zero views, not "
                "missing data)"
            )
            return {}
        if exc.kind == "invalid_route":
            raise SystemExit(
                f"error: malformed request for {title!r} on {project} "
                f"({exc.detail}). Title encoding failed -- this is a bug."
            ) from exc
        raise SystemExit(f"error [{exc.kind}]: {exc}") from exc


def fetch_series(
    study: StudyManifest,
    today: date | None = None,
    *,
    client: common.JsonFetcher | None = None,
) -> SeriesPayload:
    """Run every request in a study and build the ``series.json`` payload.

    For each language with a title: fetch project-wide views, fetch article
    views, trim both to the months actually loaded, and record any coverage
    gap or zero-view caveat as a warning.

    Args:
        study: Manifest carrying ``resolution`` (and optionally overrides).
        today: Reference date for the window; ``None`` means today (tests
            inject a fixed date so golden numbers stay reproducible).
        client: Injected transport. ``None`` builds one for this call --
            callers doing more (e.g. ``run.py all``) should pass one shared
            client (``http_client.default_client()``) instead.

    Returns:
        The :class:`payloads.SeriesPayload` written by the fetch stage.

    Raises:
        SystemExit: For an empty/invalid window or a project that returned
            no months at all (both indicate a wrong request, not no interest).
        ApiError: For API failures beyond the retry budget.
    """
    if client is None:
        client = http_client.default_client()
    resolution: Resolution = study.get("resolution") or Resolution()
    since, until, warnings = resolve_window(study, today)
    titles, sources, gaps, native_missing, title_warnings = effective_titles(study)
    warnings.extend(title_warnings)

    confirm_titles(titles, client=client)

    parameters: RequestParameters = {
        "access": study.get("access", common.DEFAULT_ACCESS),
        "agent": study.get("agent", common.DEFAULT_AGENT),
        "granularity": "monthly",
    }

    desired = common.month_range(since, until)
    # Request one month beyond the window and discard it.
    #
    # Verified: the per-article endpoint bounds the *final* bucket of a range
    # at the range end date, and because month parameters are always the 1st of
    # a month, that final bucket covers a single day rather than a whole month.
    # cs "Přerušovaný půst" 2026-08 returned 2 views when the range ended at
    # 2026080100, but 119 (the complete month) when the same range was extended
    # to 2026090100 -- reproduced 3/3 times. The aggregate endpoint does not
    # show the truncation, but overshooting it as well keeps one uniform rule
    # and costs a single extra request that is cached anyway.
    fetch_until = common.shift_month(until, FETCH_OVERSHOOT_MONTHS)
    series: dict[str, LanguageSeries] = {}

    for language, title in titles.items():
        project = common.project_for(language)
        project_views = _fetch_project_series(
            project,
            since,
            fetch_until,
            access=parameters["access"],
            agent=parameters["agent"],
            client=client,
        )
        article_raw = _fetch_article_series(
            project,
            title,
            since,
            fetch_until,
            access=parameters["access"],
            agent=parameters["agent"],
            warnings=warnings,
            client=client,
        )

        # Trim to months the project series actually has: a loaded month always
        # has project-wide views, whereas the current month may still be partial.
        available = [month for month in desired if month in project_views]
        if not available:
            raise SystemExit(
                f"error: {project} returned no months for {since}..{until}"
            )
        if len(available) < len(desired):
            dropped = [month for month in desired if month not in project_views]
            warnings.append(
                f"{language}: {len(dropped)} requested month(s) not loaded yet "
                f"({dropped[0]}..{dropped[-1]}); window trimmed to "
                f"{available[0]}..{available[-1]}"
            )

        series[language] = {
            "language": language,
            "project": project,
            "article_title": title,
            "resolved_via": sources[language],
            "native_gap": language in native_missing,
            "labels": available,
            # Zeros are omitted by the API, so a missing month means zero views.
            "article_views": [article_raw.get(month, 0) for month in available],
            "project_views": [project_views[month] for month in available],
        }
        if all(value == 0 for value in series[language]["article_views"]):
            warnings.append(
                f"{language}: {title!r} recorded zero views for every month in "
                "the window -- double-check that this is the intended article"
            )

    return {
        "generated_at": (today or date.today()).isoformat(),
        "topic": study["topic"],
        "qid": resolution.get("qid"),
        "window": {"since": since, "until": until, "months": len(desired)},
        "parameters": parameters,
        "series": series,
        "gaps": gaps,
        "warnings": warnings,
    }


def build_parser() -> argparse.ArgumentParser:
    """Build the ``fetch.py`` command-line parser."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--study", default="study.json", help="study manifest path")
    parser.add_argument("--out", default=".", help="output directory (default: cwd)")
    parser.add_argument(
        "--no-cache", action="store_true", help="ignore cached responses"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Read the manifest, fetch every series, write ``series.json``."""
    common.configure_console()
    args = build_parser().parse_args(argv)

    study = load_study(Path(args.study))
    # Composition root: one client covers every request of this run.
    with http_client.default_client(cache_enabled=not args.no_cache) as client:
        payload = fetch_series(study, client=client)
    out = Path(args.out) / "series.json"
    common.write_json(out, payload)

    print(f"wrote {out}")
    for language, item in payload["series"].items():
        print(
            f"  {language}: {item['article_title']} -- "
            f"{len(item['labels'])} months, {sum(item['article_views']):,} views"
        )
    for language in payload["gaps"]:
        print(f"  {language}: GAP (no article for this topic)")
    for warning in payload["warnings"]:
        print(f"  warning: {warning}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
