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
import resolve as resolve_mod

DEFAULT_MONTHS = 24

# Extra month requested beyond the window and then discarded, so that the
# window's final month is never the (truncated) last bucket of a range.
FETCH_OVERSHOOT_MONTHS = 1


def stamp_to_month(stamp: str) -> str:
    """``2024100100`` -> ``2024-10``."""
    return f"{stamp[0:4]}-{stamp[4:6]}"


def load_study(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise SystemExit(
            f"error: study manifest not found: {path}\n"
            "Create one with:  run.py init --topic \"...\" --langs pl,cs"
        )
    study = common.read_json(path)
    for key in ("topic", "languages"):
        if key not in study:
            raise SystemExit(f"error: study manifest is missing {key!r}: {path}")
    if study.get("granularity", common.DEFAULT_GRANULARITY) != "monthly":
        raise SystemExit(
            "error: v1 supports granularity 'monthly' only "
            f"(manifest has {study.get('granularity')!r})"
        )
    return study


def _warn_if_halves_not_aligned(
    since: str, until: str, warnings: list[str]
) -> None:
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
    study: dict[str, Any], today: date | None = None
) -> tuple[str, str, list[str]]:
    """Return ``(since, until)`` clamped to complete, available data."""
    warnings: list[str] = []
    window = study.get("window") or {}
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
    study: dict[str, Any],
) -> tuple[dict[str, str], dict[str, str], list[str], list[str], list[str]]:
    """Pick one article title per language.

    Returns ``(titles, sources, gaps, native_missing, warnings)``. An explicit
    ``overrides`` entry wins over the resolved sitelink -- substitutions are
    only ever made deliberately by a human, never automatically. ``native_missing``
    lists override languages that have no article of their own for the topic, so
    the coverage gap stays visible downstream.
    """
    overrides = study.get("overrides") or {}
    articles = (study.get("resolution") or {}).get("articles") or {}

    titles: dict[str, str] = {}
    sources: dict[str, str] = {}
    native_missing: list[str] = []
    gaps: list[str] = []
    warnings: list[str] = []

    for language in study["languages"]:
        if overrides.get(language):
            titles[language] = str(overrides[language])
            sources[language] = "override"
            if not articles.get(language):
                # The substitution exists because no native article does. Keep
                # that fact attached to the data -- an override must never
                # silently erase a coverage gap.
                native_missing.append(language)
        elif articles.get(language):
            titles[language] = str(articles[language].get("title"))
            sources[language] = "sitelink"
        else:
            gaps.append(language)
            warnings.append(
                f"{language}: no article for this topic -- reporting a coverage gap "
                "(choose a candidate and set overrides.{language} to analyse it)"
            )
    return titles, sources, gaps, native_missing, warnings


def confirm_titles(titles: dict[str, str]) -> None:
    """Fail loudly if a chosen title does not exist (protects overrides)."""
    for language, title in titles.items():
        info = resolve_mod._confirm_titles(language, [title])
        exists, _ = common.page_exists(info, title)
        if not exists:
            raise SystemExit(
                f"error: {language} article {title!r} does not exist. "
                "Fix overrides/{} in the study manifest (check the spelling, "
                "or pick a candidate from the resolution output).".format(language)
            )


def _fetch_project_series(
    project: str, since: str, until: str, *, access: str, agent: str
) -> dict[str, int]:
    url = common.aggregate_url(project, since, until, access=access, agent=agent)
    try:
        payload = common.get_json(url)
    except common.ApiError as exc:
        if exc.kind == "no_data":
            raise SystemExit(
                f"error: {project} reported no data for {since}..{until}. "
                "Check the project name and that the window is after "
                f"{common.DATA_START_MONTH}."
            ) from exc
        raise SystemExit(f"error [{exc.kind}]: {exc}") from exc
    return {
        stamp_to_month(item["timestamp"]): int(item["views"])
        for item in payload.get("items", [])
    }


def _fetch_article_series(
    project: str,
    title: str,
    since: str,
    until: str,
    *,
    access: str,
    agent: str,
    warnings: list[str],
) -> dict[str, int]:
    url = common.per_article_url(
        project, title, since, until, access=access, agent=agent
    )
    try:
        payload = common.get_json(url)
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
    return {
        stamp_to_month(item["timestamp"]): int(item["views"])
        for item in payload.get("items", [])
    }


def fetch_series(study: dict[str, Any], today: date | None = None) -> dict[str, Any]:
    """Run every request in a study and return the ``series.json`` payload."""
    since, until, warnings = resolve_window(study, today)
    titles, sources, gaps, native_missing, title_warnings = effective_titles(study)
    warnings.extend(title_warnings)

    confirm_titles(titles)

    parameters = {
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
    series: dict[str, dict[str, Any]] = {}

    for language, title in titles.items():
        project = common.project_for(language)
        project_views = _fetch_project_series(
            project, since, fetch_until, access=parameters["access"], agent=parameters["agent"]
        )
        article_raw = _fetch_article_series(
            project,
            title,
            since,
            fetch_until,
            access=parameters["access"],
            agent=parameters["agent"],
            warnings=warnings,
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
        "qid": (study.get("resolution") or {}).get("qid"),
        "window": {"since": since, "until": until, "months": len(desired)},
        "parameters": parameters,
        "series": series,
        "gaps": gaps,
        "warnings": warnings,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--study", default="study.json", help="study manifest path")
    parser.add_argument("--out", default=".", help="output directory (default: cwd)")
    parser.add_argument("--no-cache", action="store_true", help="ignore cached responses")
    return parser


def main(argv: list[str] | None = None) -> int:
    common.configure_console()
    args = build_parser().parse_args(argv)
    if args.no_cache:
        common.set_cache_enabled(False)

    study = load_study(Path(args.study))
    payload = fetch_series(study)
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
