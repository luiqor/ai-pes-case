"""Compute trend metrics, seasonality and a confidence grade from a series file.

Reads ``series.json`` (from ``fetch.py``) and writes ``analysis.json``.

Methods -- all documented with their rationale in ``references/methods.md``:

* **Growth** = second half of the window vs first half, with the two halves
  covering the *same calendar months* one year apart whenever the window length
  allows it (the odd middle month is dropped rather than skewing the seasons).
* **Normalised share** = article views / total project views x 1e6. A claim of
  growth only counts when absolute views *and* share move together; otherwise
  the change is an artefact of overall Wikipedia traffic.
* **Trend** = ordinary least squares slope and R^2 on the monthly series.
* **Seasonality** = mean views per calendar month; a peak month averaging more
  than 2x the overall mean is flagged as strongly seasonal.
* **Confidence** = a deterministic rule table (see ``grade``). The thresholds
  are *design parameters*, not facts: they are labelled as assumptions in the
  output and documented in ``references/methods.md``.
"""

from __future__ import annotations

import argparse
from datetime import date

import common
import pandas as pd
from payloads import (
    AnalysisPayload,
    Comparison,
    LanguageMetrics,
    LanguageSeries,
    SeasonalityProfile,
    SeriesPayload,
    TrendFit,
    WindowRange,
    WindowSplit,
    YoyMetrics,
)

STRONG_SEASONALITY_RATIO = 2.0
LOW_VOLUME_MEAN = 50.0
SOLID_R2 = 0.6
WEAK_R2 = 0.4
FLAT_TOLERANCE_PCT = 1.0
_LEVEL_ORDER = {"high": 0, "medium": 1, "low": 2}


# --------------------------------------------------------------------------
# Primitives
# --------------------------------------------------------------------------
def ols(values: list[float]) -> TrendFit:
    """Least-squares fit of value against month index. Returns slope + R^2.

    Args:
        values: The monthly series to fit (at least 3 points to mean much).

    Returns:
        A :class:`payloads.TrendFit`; degenerate inputs (too few points, or
        no variance in the months) yield a zero slope rather than an error.
    """
    n = len(values)
    if n < 3:
        return {"slope_per_month": 0.0, "intercept": 0.0, "r2": 0.0}
    xs = list(range(n))
    mean_x = sum(xs) / n
    mean_y = sum(values) / n
    sxx = sum((x - mean_x) ** 2 for x in xs)
    if sxx == 0:
        return {"slope_per_month": 0.0, "intercept": mean_y, "r2": 0.0}
    slope = (
        sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, values, strict=True)) / sxx
    )
    intercept = mean_y - slope * mean_x
    sst = sum((y - mean_y) ** 2 for y in values)
    sse = sum(
        (y - (intercept + slope * x)) ** 2 for x, y in zip(xs, values, strict=True)
    )
    r2 = 0.0 if sst == 0 else max(0.0, 1.0 - sse / sst)
    return {
        "slope_per_month": round(slope, 4),
        "intercept": round(intercept, 4),
        "r2": round(r2, 4),
    }


def pct_change(first: float, second: float) -> float | None:
    """Percent change from ``first`` to ``second``, or ``None`` if undefined.

    A move from zero has no defined percentage; returning ``None`` (rather
    than ``inf`` or a guess) keeps downstream ranking honest.

    Args:
        first: Baseline value (the first half's total).
        second: Value compared against the baseline (the second half).

    Returns:
        Rounded percentage, or ``None`` when ``first`` is zero.
    """
    if first == 0:
        return None
    return round((second - first) / first * 100.0, 2)


def direction(pct: float | None) -> int:
    """-1 / 0 / +1, treating moves under FLAT_TOLERANCE_PCT as flat.

    Args:
        pct: A percentage move, or ``None`` (counts as no direction).

    Returns:
        ``1`` up, ``-1`` down, ``0`` flat or undetermined.
    """
    if pct is None or abs(pct) < FLAT_TOLERANCE_PCT:
        return 0
    return 1 if pct > 0 else -1


def _growth_share_sort_key(item: tuple[str, LanguageMetrics]) -> float:
    """Sort key for the by-growth ranking: share growth, best first.

    Languages whose YoY share growth is missing sort to the bottom rather
    than crashing ``sorted`` on ``None`` (the floor of -999 is below any
    reachable percentage, since non-negative views can fall at most -100%).
    """
    share_pct = item[1]["yoy"].get("share_pct")
    return share_pct if share_pct is not None else -999.0


def _rank_by_growth(
    metrics: dict[str, LanguageMetrics],
) -> list[tuple[str, LanguageMetrics]]:
    """The by-growth ranking: languages with YoY data, best share growth first.

    The **one** implementation shared by the headline verdict and
    ``comparison.by_growth_share_pct``: ranking the same data twice with two
    different keys could let the sentence and the table disagree.
    """
    return sorted(
        (
            (language, metric)
            for language, metric in metrics.items()
            if metric["yoy"].get("available")
        ),
        key=_growth_share_sort_key,
        reverse=True,
    )


def split_windows(labels: list[str]) -> WindowSplit:
    """Split into two equal halves for a year-over-year style comparison.

    For an odd number of months the middle month is dropped so that both halves
    still cover identical calendar months (e.g. Oct-Aug vs Oct-Aug).

    Args:
        labels: The window's month labels, chronological.

    Returns:
        A :class:`payloads.WindowSplit`; when the window is too short only
        ``available`` (false) and a ``reason`` are present.
    """
    n = len(labels)
    k = n // 2
    if k < 6:
        return {"available": False, "reason": "window shorter than 12 months"}
    first = labels[:k]
    second = labels[n - k :]
    aligned = [m[5:] for m in first] == [m[5:] for m in second]
    dropped = labels[k : n - k]
    return {
        "available": True,
        "months_each": k,
        "first_labels": first,
        "second_labels": second,
        "first_range": f"{first[0]}..{first[-1]}",
        "second_range": f"{second[0]}..{second[-1]}",
        "aligned": aligned,
        "dropped_months": dropped,
    }


def seasonality_profile(labels: list[str], values: list[int]) -> SeasonalityProfile:
    """Mean views per calendar month, plus a peak/overall dominance ratio.

    Args:
        labels: Month labels (``YYYY-MM``), chronological.
        values: Views for each month, aligned with ``labels``.

    Returns:
        A :class:`payloads.SeasonalityProfile`. With under 12 months the
        profile is unavailable (``reason`` explains why); under 24 months it
        is measured but marked ``reliable: false`` (less than two cycles).
    """
    if len(labels) < 12:
        return {
            "available": False,
            "reliable": False,
            "reason": "needs at least 12 months to estimate a seasonal profile",
        }
    frame = pd.DataFrame(
        {"month": pd.to_datetime([f"{label}-01" for label in labels]), "views": values}
    )
    grouped = frame.groupby(frame["month"].dt.month)["views"].mean()
    profile = {
        int(str(month)): round(float(mean), 1) for month, mean in grouped.items()
    }

    overall = sum(values) / len(values)
    peak_month = max(profile, key=lambda m: profile[m])
    trough_month = min(profile, key=lambda m: profile[m])
    ratio = round(profile[peak_month] / overall, 3) if overall > 0 else 0.0
    return {
        "available": True,
        "reliable": len(labels) >= 24,
        "cycles": len(labels) // 12,
        "peak_month": peak_month,
        "peak_mean": profile[peak_month],
        "trough_month": trough_month,
        "trough_mean": profile[trough_month],
        "overall_mean": round(overall, 1),
        "peak_ratio": ratio,
        "profile": profile,
    }


def grade(
    *,
    months: int,
    aligned: bool,
    direction_agrees: bool,
    r2_share: float,
    strong_seasonality: bool,
    possible_seasonality: bool,
    mean_views: float,
) -> tuple[str, list[str]]:
    """Deterministic confidence rule table.

    Starts at "high" and downgrades; every downgrade is reported so the reader
    can see exactly why a grade was (not) given. Thresholds are design
    parameters documented in references/methods.md.

    Args:
        months: Window length in months.
        aligned: Whether the two halves cover the same calendar months.
        direction_agrees: Whether absolute and normalised moves point the
            same way (a disagreement means the move is traffic-driven).
        r2_share: R^2 of the linear fit on the normalised share.
        strong_seasonality: A reliable, >=2x seasonal peak was measured.
        possible_seasonality: A >=2x peak was seen but the window is too
            short to call it reliable (reported, never graded).
        mean_views: Mean monthly article views (low volume is noisy).

    Returns:
        ``(grade, reasons)`` -- one of ``high``/``medium``/``low`` plus the
        human-readable justification, best reason first for ``high``.
    """
    level = "high"
    reasons: list[str] = []

    def downgrade(new_level: str, reason: str) -> None:
        nonlocal level
        reasons.append(reason)
        if _LEVEL_ORDER[new_level] > _LEVEL_ORDER[level]:
            level = new_level

    if months < 12:
        downgrade("low", "window shorter than 12 months")
    if not aligned:
        downgrade("medium", "comparison halves are not the same calendar months")
    if not direction_agrees:
        downgrade(
            "low",
            "absolute views and normalised share disagree -- the change tracks "
            "overall wiki traffic, not interest in this topic",
        )
    if strong_seasonality:
        downgrade(
            "medium",
            f"strongly seasonal (peak month averages "
            f"{STRONG_SEASONALITY_RATIO}x+ the overall mean)",
        )
        if r2_share < WEAK_R2:
            downgrade("low", "seasonality dominates and the linear fit is weak")
    elif r2_share < WEAK_R2:
        downgrade("medium", f"weak linear fit on share (R2 {r2_share:.2f})")
    elif r2_share < SOLID_R2:
        downgrade("medium", f"moderate linear fit on share (R2 {r2_share:.2f})")
    if possible_seasonality:
        reasons.append(
            "seasonality looks strong but the window spans under two full "
            "cycles, so it is not reliably estimated"
        )
    if months < 24:
        downgrade("medium", "fewer than 24 months, so under two full seasonal cycles")
    if mean_views < LOW_VOLUME_MEAN:
        downgrade(
            "low",
            f"very low volume (mean {mean_views:.0f} views/month) -- single "
            "events dominate the series",
        )

    if level == "high":
        reasons.insert(
            0,
            f"{months} months, calendar-aligned halves, direction holds on both "
            f"metrics, R2 {r2_share:.2f}",
        )
    return level, reasons


# --------------------------------------------------------------------------
# Per-language metrics
# --------------------------------------------------------------------------
def analyze_language(language: str, item: LanguageSeries) -> LanguageMetrics:
    """Compute every metric for one language's series.

    Args:
        language: Language code (also the key in the output metrics map).
        item: One entry of ``series.json`` (labels + both view series).

    Returns:
        The :class:`payloads.LanguageMetrics` record: totals, share, growth,
        trend fits, seasonality, flags and the graded confidence.
    """
    labels: list[str] = item["labels"]
    article: list[int] = item["article_views"]
    project: list[int] = item["project_views"]
    months = len(labels)

    share = [(a / p * 1e6) if p else 0.0 for a, p in zip(article, project, strict=True)]
    article_total = float(sum(article))
    project_total = float(sum(project))

    # --- growth: first half vs second half ----------------------------------
    split = split_windows(labels)
    yoy: YoyMetrics = {"available": False}
    article_pct: float | None = None
    share_pct: float | None = None
    aligned = True
    if split.get("available"):
        k = split["months_each"]
        first_article = float(sum(article[:k]))
        second_article = float(sum(article[months - k :]))
        first_project = float(sum(project[:k]))
        second_project = float(sum(project[months - k :]))
        first_share = (first_article / first_project * 1e6) if first_project else 0.0
        second_share = (
            (second_article / second_project * 1e6) if second_project else 0.0
        )

        article_pct = pct_change(first_article, second_article)
        share_pct = pct_change(first_share, second_share)
        aligned = bool(split["aligned"])
        yoy = {
            "available": True,
            "months_each": k,
            "first_range": split["first_range"],
            "second_range": split["second_range"],
            "aligned": aligned,
            "dropped_months": split["dropped_months"],
            "article_first": int(first_article),
            "article_second": int(second_article),
            "article_pct": article_pct,
            "project_first": int(first_project),
            "project_second": int(second_project),
            "project_pct": pct_change(first_project, second_project),
            "share_first_ppm": round(first_share, 4),
            "share_second_ppm": round(second_share, 4),
            "share_pct": share_pct,
        }

    trend_article = ols([float(v) for v in article])
    trend_share = ols(share)
    seasonal = seasonality_profile(labels, article)
    # Two distinct notions, deliberately kept apart:
    #   * observed  -- a factual description of the data (report it as a caveat);
    #   * strong    -- usable to grade confidence, which requires at least two
    #                  full seasonal cycles. Penalising a confidence grade with
    #                  an estimate we have already labelled unreliable would be
    #                  inconsistent.
    seasonality_observed = bool(
        seasonal.get("available") and seasonal["peak_ratio"] >= STRONG_SEASONALITY_RATIO
    )
    strong_seasonality = seasonality_observed and bool(seasonal.get("reliable"))
    direction_agrees = direction(article_pct) == direction(share_pct)
    mean_views = article_total / months if months else 0.0
    small_volume = mean_views < LOW_VOLUME_MEAN

    confidence, reasons = grade(
        months=months,
        aligned=aligned,
        direction_agrees=direction_agrees,
        r2_share=trend_share["r2"],
        strong_seasonality=strong_seasonality,
        possible_seasonality=seasonality_observed and not strong_seasonality,
        mean_views=mean_views,
    )

    return {
        "language": language,
        "article_title": item["article_title"],
        "project": item["project"],
        "resolved_via": item.get("resolved_via"),
        "native_gap": bool(item.get("native_gap", False)),
        "months": months,
        "article_total": int(article_total),
        "project_total": int(project_total),
        "share_ppm": round(article_total / project_total * 1e6, 4)
        if project_total
        else 0.0,
        "mean_monthly_views": round(mean_views, 1),
        "yoy": yoy,
        "trend": {"article": trend_article, "share": trend_share},
        "seasonality": seasonal,
        "flags": {
            "direction_agrees": direction_agrees,
            "seasonality_observed": seasonality_observed,
            "strong_seasonality": strong_seasonality,
            "small_volume": small_volume,
        },
        "confidence": confidence,
        "confidence_reasons": reasons,
    }


# --------------------------------------------------------------------------
# Composition
# --------------------------------------------------------------------------
def build_headline(
    topic: str,
    window: WindowRange,
    metrics: dict[str, LanguageMetrics],
    gaps: list[str],
) -> str:
    """Write the one-sentence verdict: growth per language plus every grade.

    Args:
        topic: The study topic, quoted into the sentence.
        window: Resolved window (used for the ``since..until`` span).
        metrics: Per-language metrics, keyed by language code.
        gaps: Languages with no article at all (appended as unmeasurable).

    Returns:
        A single self-contained sentence; the report prints it verbatim.
    """
    span = f"{window['since']}..{window['until']}"
    ranked = _rank_by_growth(metrics)
    if not ranked:
        return (
            f"No language edition has enough data to assess interest "
            f"in {topic!r} over {span}."
        )

    parts = []
    for lang, m in ranked:
        share_pct = m["yoy"]["share_pct"]
        article_pct = m["yoy"]["article_pct"]
        parts.append(
            f"{lang} {share_pct:+.1f}% share ({article_pct:+.1f}% absolute)"
            if share_pct is not None and article_pct is not None
            else f"{lang} n/a"
        )
    grades = ", ".join(f"{lang} {m['confidence']}" for lang, m in ranked)
    sentence = (
        f"Normalised interest in {topic!r} over {span} (second half vs first half): "
        + "; ".join(parts)
        + f". Confidence: {grades}."
    )
    substituted = [
        f"{lang} uses '{m['article_title']}' (no native article exists)"
        for lang, m in metrics.items()
        if m.get("resolved_via") == "override" and m.get("native_gap")
    ]
    if substituted:
        sentence += " Substitute in use: " + "; ".join(substituted) + "."
    if gaps:
        sentence += f" No article exists in: {', '.join(gaps)} -- not measurable."
    return sentence


def build_limitations(
    payload: SeriesPayload,
    metrics: dict[str, LanguageMetrics],
    gaps: list[str],
) -> list[str]:
    """Assemble every caveat the reader must weigh, in report order.

    Covers what pageviews cannot say, the single-article proxy, the request
    parameters used, the growth definition, coverage gaps, manual
    substitutions and any seasonal signal.

    Args:
        payload: The ``series.json`` payload (parameters + series detail).
        metrics: Per-language metrics for the seasonal flags.
        gaps: Languages with no article for this topic.

    Returns:
        Limitations, most important first; the report de-duplicates them
        against warnings and assumptions.
    """
    parameters = payload["parameters"]
    limitations = [
        "Pageviews measure article reads, not willingness to pay -- treat this as "
        "a direction indicator, not a market size.",
        "Interest is proxied by one article per language edition; how much of the "
        "topic each article covers can differ between editions.",
        f"Requests used access={parameters['access']}, "
        f"agent={parameters['agent']} (bots excluded); Wikimedia rate "
        "limits were not probed, so requests are sequential with a fixed delay.",
        "Growth compares the second half of the window with the first half "
        "(design choice; see references/methods.md).",
    ]
    if gaps:
        limitations.insert(
            0,
            f"No Wikipedia article exists for this topic in: {', '.join(gaps)}. "
            "Coverage gaps are reported, never filled with a substitute article.",
        )
    substituted = [
        f"{language} = '{item['article_title']}' ("
        + (
            "no native article exists for this topic"
            if item.get("native_gap")
            else "chosen explicitly"
        )
        + ")"
        for language, item in payload["series"].items()
        if item.get("resolved_via") == "override"
    ]
    if substituted:
        limitations.append(
            "Manual substitution in effect: "
            + "; ".join(substituted)
            + ". A substitute may cover a broader or different scope than the "
            "topic, so cross-language comparisons involving it are not "
            "like-for-like."
        )
    seasonal = [
        f"{lang} peaks in calendar month {m['seasonality']['peak_month']} "
        f"({m['seasonality']['peak_ratio']}x the overall mean)"
        for lang, m in metrics.items()
        if m["flags"].get("seasonality_observed")
    ]
    if seasonal:
        limitations.append(
            "Seasonal signal detected -- "
            + "; ".join(seasonal)
            + ". A single seasonal cycle can dominate a short window."
        )
    return limitations


def analyze(payload: SeriesPayload) -> AnalysisPayload:
    """Turn ``series.json`` into ``analysis.json``.

    Runs every per-language metric, assembles the cross-language rankings,
    and attaches the headline plus the assumptions/limitations that must
    travel with any claim made from these numbers.

    Args:
        payload: The ``series.json`` structure from ``fetch.py``.

    Returns:
        The :class:`payloads.AnalysisPayload` written to ``analysis.json``.

    Raises:
        KeyError: If the payload lacks a required section -- in practice only
        for a hand-edited file (the read boundary validates it first).
    """
    metrics = {
        language: analyze_language(language, item)
        for language, item in payload["series"].items()
    }
    gaps = payload.get("gaps", [])
    warnings = list(payload.get("warnings", []))

    for language, item in payload["series"].items():
        share = [
            (a / p * 1e6) if p else 0.0
            for a, p in zip(item["article_views"], item["project_views"], strict=True)
        ]
        metrics[language]["series"] = {
            "labels": item["labels"],
            "article_views": item["article_views"],
            "project_views": item["project_views"],
            "share_ppm": [round(value, 4) for value in share],
        }

    ordered = sorted(metrics.items(), key=lambda kv: kv[1]["share_ppm"], reverse=True)
    growth_ranked = _rank_by_growth(metrics)
    comparison: Comparison = {
        "by_share_ppm": [
            {"language": lang, "share_ppm": m["share_ppm"]} for lang, m in ordered
        ],
        "by_growth_share_pct": [
            {
                "language": lang,
                "share_pct": m["yoy"]["share_pct"],
                "article_pct": m["yoy"]["article_pct"],
            }
            for lang, m in growth_ranked
        ],
        "highest_share": ordered[0][0] if ordered else None,
        "fastest_growth": growth_ranked[0][0] if growth_ranked else None,
    }

    return {
        "generated_at": payload.get("generated_at") or date.today().isoformat(),
        "topic": payload["topic"],
        "qid": payload.get("qid"),
        "window": payload["window"],
        "parameters": payload["parameters"],
        "headline": build_headline(payload["topic"], payload["window"], metrics, gaps),
        "metrics": metrics,
        "comparison": comparison,
        "gaps": gaps,
        "warnings": warnings,
        "assumptions": [
            # NOTE: the growth definition lives in `limitations` (with its
            # references/methods.md pointer), not here -- stating it twice made
            # the report print the same bullet twice and cost one-page space.
            "Share = article views / total project views x 1e6; growth counts only "
            "when absolute and normalised direction agree.",
            "Confidence thresholds (R2 0.6/0.4, seasonality 2x, 24 months, 50 "
            "views/month) are design parameters, tuned on recorded fixtures.",
        ],
        "limitations": build_limitations(payload, metrics, gaps),
    }


def build_parser() -> argparse.ArgumentParser:
    """Build the ``analyze.py`` command-line parser."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--series", default="series.json", help="input from fetch.py")
    parser.add_argument("--out", default="analysis.json", help="output path")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Read ``series.json``, write ``analysis.json``, print the headline."""
    common.configure_console()
    args = build_parser().parse_args(argv)
    payload = common.read_json(args.series)
    result = analyze(payload)
    common.write_json(args.out, result)
    print(f"wrote {args.out}")
    print(f"  {result['headline']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
