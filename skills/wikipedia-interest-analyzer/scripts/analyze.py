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
import i18n
import pandas as pd
from payloads import (
    AnalysisPayload,
    Comparison,
    CriteriaResult,
    CriteriaSummary,
    CriterionVerdict,
    LanguageMetrics,
    LanguageSeries,
    MessageRef,
    SeasonalityProfile,
    SeriesPayload,
    SuccessRule,
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


def _criterion_value(
    metric: LanguageMetrics, criterion: str
) -> float | None:
    """The number one language scores on for ``criteria.rank_by``.

    Returns ``None`` when the metric was not measured for this language (a
    coverage gap, or the layer feeding it was never fetched) -- the ranking
    then omits that language instead of inventing a value for it.
    """
    if criterion == "share_ppm":
        return float(metric["share_ppm"])
    if criterion == "yoy_share":
        return metric["yoy"].get("share_pct")
    if criterion == "mobile_share":
        split = metric.get("access_split")
        return split["mobile_pct"] if split else None
    if criterion == "bot_share":
        return metric.get("bot_share_pct")
    return None


def _rank_by_criterion(
    metrics: dict[str, LanguageMetrics], criterion: str
) -> list[str]:
    """Order the measured languages best-first for one ranking criterion.

    Every criterion except ``bot_share`` is a "more is better" quantity;
    ``bot_share`` ranks **ascending** -- a cleaner, more human signal first --
    which is the only reading that makes it useful for choosing an audience.
    Ties break on the language code so the order is deterministic.
    """
    scored = [
        (language, value)
        for language, metric in metrics.items()
        if (value := _criterion_value(metric, criterion)) is not None
    ]
    if criterion == "bot_share":
        scored.sort(key=lambda kv: (kv[1], kv[0]))
    else:
        scored.sort(key=lambda kv: (-kv[1], kv[0]))
    return [language for language, _ in scored]


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

    result: LanguageMetrics = {
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

    # --- optional data layers (present only when the study requested them) ---
    access_views = item.get("access_views")
    if access_views:
        # Verified live (2026-09-27): the three channels sum to the base
        # all-access series exactly, so the base total is the denominator.
        desktop = float(sum(access_views.get("desktop", [])))
        mobile_web = float(sum(access_views.get("mobile-web", [])))
        mobile_app = float(sum(access_views.get("mobile-app", [])))
        total = article_total or (desktop + mobile_web + mobile_app)

        def pct(part: float) -> float:
            return round(part / total * 100, 2) if total else 0.0

        result["access_split"] = {
            "total_views": int(article_total),
            "desktop_pct": pct(desktop),
            "mobile_web_pct": pct(mobile_web),
            "mobile_app_pct": pct(mobile_app),
            "mobile_pct": pct(mobile_web + mobile_app),
        }

    all_agents = item.get("all_agents_views")
    if all_agents is not None:
        # all-agents = user + spider + automated (verified exact), so the
        # difference is the non-human share. Clamped: a filter disagreement
        # must never print a negative bot share.
        bot_total = float(sum(all_agents))
        if bot_total > 0:
            non_user = max(bot_total - article_total, 0.0)
            result["bot_share_pct"] = round(non_user / bot_total * 100, 2)

    if item.get("top_rank"):
        result["top_rank"] = item["top_rank"]

    return result


# --------------------------------------------------------------------------
# The user's success criteria (study.json "criteria.success")
#
# A rule *compares* a metric this stage already computed -- it never
# computes a new one -- so the verdict stays data-grounded (Rule 4) and the
# whole evaluation runs offline, like the rest of this stage.
# --------------------------------------------------------------------------
_GRADE_RANK = {"low": 0, "medium": 1, "high": 2}


def _metric_value(
    metrics: LanguageMetrics, metric: str
) -> tuple[float | str | None, str | None]:
    """Read one closed-list metric; ``(value, None)`` or ``(None, why)``.

    ``why`` is a short reason the report can print beside a rule that could
    not be evaluated: an unmeasurable criterion is reported as such, never
    as a failure.
    """
    if metric == "share_ppm":
        return metrics["share_ppm"], None
    if metric == "article_total":
        return metrics["article_total"], None
    if metric == "mean_monthly_views":
        return metrics["mean_monthly_views"], None
    if metric == "yoy_share_pct":
        value = metrics["yoy"].get("share_pct")
        return (value, None) if value is not None else (None, "growth unavailable")
    if metric == "yoy_article_pct":
        value = metrics["yoy"].get("article_pct")
        return (value, None) if value is not None else (None, "growth unavailable")
    if metric == "trend_r2":
        return metrics["trend"]["share"]["r2"], None
    if metric == "confidence":
        return metrics["confidence"], None
    if metric == "mobile_pct":
        split = metrics.get("access_split")
        if split is None:
            return None, "layer not measured"
        return split["mobile_pct"], None
    if metric == "bot_share_pct":
        value = metrics.get("bot_share_pct")
        if value is None:
            return None, "layer not measured"
        return value, None
    if metric == "top_rank":
        rank = metrics.get("top_rank")
        if rank is None:
            return None, "layer not measured"
        if rank["rank"] is None:
            return None, "outside the top list"
        return rank["rank"], None
    # Anything outside the closed list is a hand-edited series.json; report
    # it as unmeasurable instead of pretending the rule was never asked.
    return None, "unknown metric"


def _compare(value: float | str, op: str, threshold: float | str) -> bool:
    """Apply the rule's operator; ``confidence`` compares on its grade order."""
    if isinstance(value, str) and isinstance(threshold, str):
        left = float(_GRADE_RANK[value])
        right = float(_GRADE_RANK[threshold])
    else:
        left = float(value)
        right = float(threshold)
    if op == ">=":
        return left >= right
    if op == ">":
        return left > right
    if op == "<=":
        return left <= right
    if op == "<":
        return left < right
    return left == right


def _evaluate_criteria(
    rules: list[SuccessRule],
    metrics: dict[str, LanguageMetrics],
    gaps: list[str],
) -> CriteriaResult:
    """Grade every rule for every language; ``passed`` stays ``None`` when
    the metric could not be measured (gap, missing layer, absent growth)."""
    verdicts: dict[str, dict[str, CriterionVerdict]] = {}
    summary: dict[str, CriteriaSummary] = {}
    for language in [*metrics, *(g for g in gaps if g not in metrics)]:
        if language in gaps:
            verdicts[language] = {
                str(rule["id"]): {
                    "value": None,
                    "passed": None,
                    "reason": "no article",
                }
                for rule in rules
            }
            summary[language] = {
                "met": 0,
                "total": len(rules),
                "not_evaluable": len(rules),
            }
            continue
        language_verdicts: dict[str, CriterionVerdict] = {}
        met = 0
        not_evaluable = 0
        for rule in rules:
            value, reason = _metric_value(metrics[language], str(rule["metric"]))
            if value is None:
                passed: bool | None = None
                reason = reason or "not measurable"
            else:
                try:
                    passed = _compare(value, str(rule["op"]), rule["value"])
                except (KeyError, TypeError, ValueError):
                    passed, reason = None, "not measurable"
            if passed is None:
                not_evaluable += 1
                language_verdicts[str(rule["id"])] = {
                    "value": value,
                    "passed": None,
                    "reason": reason or "not measurable",
                }
            else:
                met += int(passed)
                language_verdicts[str(rule["id"])] = {
                    "value": value,
                    "passed": passed,
                }
        verdicts[language] = language_verdicts
        summary[language] = {
            "met": met,
            "total": len(rules),
            "not_evaluable": not_evaluable,
        }
    return {"success": list(rules), "verdicts": verdicts, "summary": summary}


# --------------------------------------------------------------------------
# Composition
# --------------------------------------------------------------------------
def build_headline(
    topic: str,
    window: WindowRange,
    metrics: dict[str, LanguageMetrics],
    gaps: list[str],
) -> MessageRef:
    """Compose the one-sentence verdict as a translatable message ref.

    Args:
        topic: The study topic, quoted into the sentence.
        window: Resolved window (used for the ``since..until`` span).
        metrics: Per-language metrics, keyed by language code.
        gaps: Languages with no article at all (appended as unmeasurable).

    Returns:
        A :class:`payloads.MessageRef`. Its English rendering
        (``i18n.english().render(ref)``) is what goes into ``analysis.json``
        as ``headline``; the report renders the same ref in the requested
        report language, so the two cannot disagree.
    """
    span = f"{window['since']}..{window['until']}"
    ranked = _rank_by_growth(metrics)
    if not ranked:
        return {
            "id": "headline.none",
            "params": {"topic": repr(topic), "span": span},
        }

    parts: list[MessageRef] = []
    for lang, m in ranked:
        share_pct = m["yoy"]["share_pct"]
        article_pct = m["yoy"]["article_pct"]
        if share_pct is not None and article_pct is not None:
            parts.append(
                {
                    "id": "headline.part",
                    "params": {
                        "lang": lang,
                        "share_pct": f"{share_pct:+.1f}%",
                        "article_pct": f"{article_pct:+.1f}%",
                    },
                }
            )
        else:
            parts.append({"id": "headline.part_na", "params": {"lang": lang}})
    grades: list[MessageRef] = [
        {
            "id": "headline.grade",
            "params": {
                "lang": lang,
                # The grade is a data value in analysis.json ("medium"); the
                # message ref is what lets the report show it in report words.
                "confidence": {"id": f"confidence.{m['confidence']}"},
            },
        }
        for lang, m in ranked
    ]
    main: MessageRef = {
        "id": "headline.main",
        "params": {
            "topic": repr(topic),
            "span": span,
            "parts": {"items": parts, "sep": "join.semicolon"},
            "grades": {"items": grades, "sep": "join.comma"},
        },
    }

    substituted: list[MessageRef] = [
        {
            "id": "headline.substitute",
            "params": {"lang": lang, "title": m["article_title"]},
        }
        for lang, m in metrics.items()
        if m.get("resolved_via") == "override" and m.get("native_gap")
    ]
    if not substituted and not gaps:
        return main

    # Optional sentences are appended with a space, so the English headline
    # stays byte-for-byte what the rule above composes.
    sentences: list[MessageRef] = [main]
    if substituted:
        sentences.append(
            {
                "id": "headline.substitutes",
                "params": {"items": {"items": substituted, "sep": "join.semicolon"}},
            }
        )
    if gaps:
        sentences.append(
            {
                "id": "headline.gaps",
                "params": {"langs": {"items": gaps, "sep": "join.comma"}},
            }
        )
    return {"concat": sentences}


def build_assumptions() -> list[MessageRef]:
    """The two standing assumptions, as refs (fixed order, always present).

    NOTE: the growth definition lives in ``build_limitations``, not here --
    stating it twice made the report print the same bullet twice and cost
    one-page space.
    """
    return [
        {"id": "assumption.share"},
        {"id": "assumption.thresholds"},
    ]


def build_limitations(
    payload: SeriesPayload,
    metrics: dict[str, LanguageMetrics],
    gaps: list[str],
) -> list[MessageRef]:
    """Assemble every caveat the reader must weigh, in report order.

    Covers what pageviews cannot say, the single-article proxy, the request
    parameters used, the growth definition, coverage gaps, manual
    substitutions and any seasonal signal.

    Args:
        payload: The ``series.json`` payload (parameters + series detail).
        metrics: Per-language metrics for the seasonal flags.
        gaps: Languages with no article for this topic.

    Returns:
        Limitations as message refs, most important first; the report renders
        them in the report language and de-duplicates them against warnings
        and assumptions after rendering.
    """
    parameters = payload["parameters"]
    limitations: list[MessageRef] = [
        {"id": "limitation.pageviews"},
        {"id": "limitation.proxy"},
        {
            "id": "limitation.requests",
            "params": {
                "access": parameters["access"],
                "agent": parameters["agent"],
            },
        },
        {"id": "limitation.growth"},
    ]
    if gaps:
        limitations.insert(
            0,
            {
                "id": "limitation.gaps",
                "params": {"langs": {"items": gaps, "sep": "join.comma"}},
            },
        )
    substituted: list[MessageRef] = [
        {
            "id": "limitation.substitute_item",
            "params": {
                "lang": language,
                "title": item["article_title"],
                "reason": {
                    "id": (
                        "limitation.reason_native"
                        if item.get("native_gap")
                        else "limitation.reason_chosen"
                    )
                },
            },
        }
        for language, item in payload["series"].items()
        if item.get("resolved_via") == "override"
    ]
    if substituted:
        limitations.append(
            {
                "id": "limitation.substitutes",
                "params": {"items": {"items": substituted, "sep": "join.semicolon"}},
            }
        )
    seasonal: list[MessageRef] = [
        {
            "id": "limitation.seasonal_item",
            "params": {
                "lang": lang,
                "month": m["seasonality"]["peak_month"],
                "ratio": m["seasonality"]["peak_ratio"],
            },
        }
        for lang, m in metrics.items()
        if m["flags"].get("seasonality_observed")
    ]
    if seasonal:
        limitations.append(
            {
                "id": "limitation.seasonal",
                "params": {"items": {"items": seasonal, "sep": "join.semicolon"}},
            }
        )
    # Layer caveats travel with the metrics they describe: present only when
    # the data is (a requested layer that produced nothing says nothing).
    layer_refs: list[MessageRef] = []
    if any(m.get("access_split") for m in metrics.values()):
        layer_refs.append({"id": "limitation.layer_access"})
    if any("bot_share_pct" in m for m in metrics.values()):
        layer_refs.append({"id": "limitation.layer_bot"})
    if any(m.get("top_rank") for m in metrics.values()):
        layer_refs.append({"id": "limitation.layer_top"})
    if len(layer_refs) == 3:
        # One line instead of three: these bullets share the strictly-one-page
        # PDF's last points, and three separate ones pushed a real study over
        # the edge by 1pt. The wording says exactly what was measured.
        limitations.append({"id": "limitation.layers_all"})
    else:
        limitations.extend(layer_refs)
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
    # The user's ranking criterion (study.json "criteria.rank_by"), echoed
    # through series.json parameters. Absent when no criterion was given, so
    # a study without one renders exactly as it always did.
    rank_by = payload["parameters"].get("rank_by")
    if rank_by:
        comparison["ranked_by"] = {
            "criterion": str(rank_by),
            "order": _rank_by_criterion(metrics, str(rank_by)),
        }

    # The user's own success thresholds (study.json "criteria.success"),
    # echoed through series.json parameters like rank_by. Absent means the
    # analysis keeps exactly the keys it always had -- no new one, so a
    # study without criteria is byte-for-byte what it was.
    success = payload["parameters"].get("success")
    criteria_result = (
        _evaluate_criteria(list(success), metrics, gaps) if success else None
    )

    # One composition, two consumers: the English strings below are the
    # *rendering of* the refs stored beside them, so a report in any other
    # language says exactly what analysis.json says -- only in other words.
    english = i18n.english()
    headline_ref = build_headline(payload["topic"], payload["window"], metrics, gaps)
    assumption_refs = build_assumptions()
    limitation_refs = build_limitations(payload, metrics, gaps)

    result: AnalysisPayload = {
        "generated_at": payload.get("generated_at") or date.today().isoformat(),
        "topic": payload["topic"],
        "qid": payload.get("qid"),
        "window": payload["window"],
        "parameters": payload["parameters"],
        "headline": english.render(headline_ref),
        "headline_i18n": headline_ref,
        "metrics": metrics,
        "comparison": comparison,
        "gaps": gaps,
        "warnings": warnings,
        "assumptions": [english.render(ref) for ref in assumption_refs],
        "assumptions_i18n": assumption_refs,
        "limitations": [english.render(ref) for ref in limitation_refs],
        "limitations_i18n": limitation_refs,
    }
    if criteria_result is not None:
        result["criteria"] = criteria_result
    return result


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
