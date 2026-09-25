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
from typing import Any

import pandas as pd

import common

STRONG_SEASONALITY_RATIO = 2.0
LOW_VOLUME_MEAN = 50.0
SOLID_R2 = 0.6
WEAK_R2 = 0.4
FLAT_TOLERANCE_PCT = 1.0
_LEVEL_ORDER = {"high": 0, "medium": 1, "low": 2}


# --------------------------------------------------------------------------
# Primitives
# --------------------------------------------------------------------------
def ols(values: list[float]) -> dict[str, float]:
    """Least-squares fit of value against month index. Returns slope + R^2."""
    n = len(values)
    if n < 3:
        return {"slope_per_month": 0.0, "intercept": 0.0, "r2": 0.0}
    xs = list(range(n))
    mean_x = sum(xs) / n
    mean_y = sum(values) / n
    sxx = sum((x - mean_x) ** 2 for x in xs)
    if sxx == 0:
        return {"slope_per_month": 0.0, "intercept": mean_y, "r2": 0.0}
    slope = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, values)) / sxx
    intercept = mean_y - slope * mean_x
    sst = sum((y - mean_y) ** 2 for y in values)
    sse = sum((y - (intercept + slope * x)) ** 2 for x, y in zip(xs, values))
    r2 = 0.0 if sst == 0 else max(0.0, 1.0 - sse / sst)
    return {
        "slope_per_month": round(slope, 4),
        "intercept": round(intercept, 4),
        "r2": round(r2, 4),
    }


def pct_change(first: float, second: float) -> float | None:
    if first == 0:
        return None
    return round((second - first) / first * 100.0, 2)


def direction(pct: float | None) -> int:
    """-1 / 0 / +1, treating moves under FLAT_TOLERANCE_PCT as flat."""
    if pct is None or abs(pct) < FLAT_TOLERANCE_PCT:
        return 0
    return 1 if pct > 0 else -1


def split_windows(labels: list[str]) -> dict[str, Any]:
    """Split into two equal halves for a year-over-year style comparison.

    For an odd number of months the middle month is dropped so that both halves
    still cover identical calendar months (e.g. Oct-Aug vs Oct-Aug).
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


def seasonality_profile(labels: list[str], values: list[int]) -> dict[str, Any]:
    """Mean views per calendar month, plus a peak/overall dominance ratio."""
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
    profile = {int(month): round(float(mean), 1) for month, mean in grouped.items()}

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
def analyze_language(language: str, item: dict[str, Any]) -> dict[str, Any]:
    labels: list[str] = item["labels"]
    article: list[int] = item["article_views"]
    project: list[int] = item["project_views"]
    months = len(labels)

    share = [
        (a / p * 1e6) if p else 0.0 for a, p in zip(article, project)
    ]
    article_total = float(sum(article))
    project_total = float(sum(project))

    # --- growth: first half vs second half ----------------------------------
    split = split_windows(labels)
    yoy: dict[str, Any] = {"available": False}
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
        second_share = (second_article / second_project * 1e6) if second_project else 0.0

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
        "share_ppm": round(article_total / project_total * 1e6, 4) if project_total else 0.0,
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
    topic: str, window: dict[str, Any], metrics: dict[str, dict[str, Any]], gaps: list[str]
) -> str:
    span = f"{window['since']}..{window['until']}"
    with_yoy = {
        lang: m for lang, m in metrics.items() if m["yoy"].get("available")
    }
    if not with_yoy:
        return f"No language edition has enough data to assess interest in {topic!r} over {span}."

    ranked = sorted(
        with_yoy.items(),
        key=lambda kv: (kv[1]["yoy"]["share_pct"] if kv[1]["yoy"]["share_pct"] is not None else -999),
        reverse=True,
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
    payload: dict[str, Any], metrics: dict[str, dict[str, Any]], gaps: list[str]
) -> list[str]:
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
        + ("no native article exists for this topic" if item.get("native_gap")
           else "chosen explicitly")
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
            "Seasonal signal detected -- " + "; ".join(seasonal)
            + ". A single seasonal cycle can dominate a short window."
        )
    return limitations


def analyze(payload: dict[str, Any]) -> dict[str, Any]:
    metrics = {
        language: analyze_language(language, item)
        for language, item in payload["series"].items()
    }
    gaps = payload.get("gaps", [])
    warnings = list(payload.get("warnings", []))

    for language, item in payload["series"].items():
        share = [
            (a / p * 1e6) if p else 0.0
            for a, p in zip(item["article_views"], item["project_views"])
        ]
        metrics[language]["series"] = {
            "labels": item["labels"],
            "article_views": item["article_views"],
            "project_views": item["project_views"],
            "share_ppm": [round(value, 4) for value in share],
        }

    ordered = sorted(metrics.items(), key=lambda kv: kv[1]["share_ppm"], reverse=True)
    comparison = {
        "by_share_ppm": [
            {"language": lang, "share_ppm": m["share_ppm"]} for lang, m in ordered
        ],
        "by_growth_share_pct": [
            {
                "language": lang,
                "share_pct": m["yoy"]["share_pct"],
                "article_pct": m["yoy"]["article_pct"],
            }
            for lang, m in sorted(
                metrics.items(),
                key=lambda kv: (
                    kv[1]["yoy"]["share_pct"]
                    if kv[1]["yoy"].get("share_pct") is not None
                    else -999
                ),
                reverse=True,
            )
            if m["yoy"].get("available")
        ],
        "highest_share": ordered[0][0] if ordered else None,
        "fastest_growth": (
            next(
                (
                    lang
                    for lang, m in sorted(
                        metrics.items(),
                        key=lambda kv: (
                            kv[1]["yoy"]["share_pct"]
                            if kv[1]["yoy"].get("share_pct") is not None
                            else -999
                        ),
                        reverse=True,
                    )
                    if m["yoy"].get("available")
                ),
                None,
            )
        ),
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
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--series", default="series.json", help="input from fetch.py")
    parser.add_argument("--out", default="analysis.json", help="output path")
    return parser


def main(argv: list[str] | None = None) -> int:
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
