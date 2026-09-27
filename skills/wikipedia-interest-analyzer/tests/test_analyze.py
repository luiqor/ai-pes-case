"""Analysis: golden trend metrics, statistics primitives, and the grade table.

The golden numbers were verified by hand against production data before being
written down here; if any of them drift, the maths or the window handling has
regressed.
"""

from __future__ import annotations

from typing import Any

import analyze as analyze_mod
import common
import pytest


# --------------------------------------------------------------- primitives --
def test_ols_recovers_a_perfect_line():
    fit = analyze_mod.ols([1.0, 2.0, 3.0, 4.0, 5.0])
    assert fit["slope_per_month"] == pytest.approx(1.0)
    assert fit["r2"] == pytest.approx(1.0)


def test_ols_on_a_constant_series_has_no_explained_variance():
    fit = analyze_mod.ols([5.0] * 12)
    assert fit["r2"] == 0.0
    assert fit["slope_per_month"] == 0.0


def test_ols_handles_too_few_points():
    assert analyze_mod.ols([1.0, 2.0])["r2"] == 0.0


@pytest.mark.parametrize(
    ("first", "second", "expected"),
    [
        pytest.param(200, 100, -50.0, id="halved"),
        pytest.param(0, 5, None, id="zero-baseline-is-not-a-percentage"),
    ],
)
def test_pct_change_measures_the_move_against_the_first_value(first, second, expected):
    assert analyze_mod.pct_change(first, second) == expected


@pytest.mark.parametrize(
    ("pct", "expected"),
    [
        pytest.param(-17.27, -1, id="clear-decline"),
        pytest.param(12.0, 1, id="clear-growth"),
        # Moves under the flat tolerance are treated as flat, not as a trend.
        pytest.param(-0.4, 0, id="inside-the-flat-tolerance"),
        pytest.param(None, 0, id="no-data-is-flat"),
    ],
)
def test_direction_counts_only_moves_outside_the_flat_tolerance(pct, expected):
    assert analyze_mod.direction(pct) == expected


# ------------------------------------------------------------- window split ---
def test_odd_window_drops_the_middle_month_and_stays_calendar_aligned():
    labels = common.month_range("2024-10", "2026-08")
    split = analyze_mod.split_windows(labels)

    assert split["available"] is True
    assert split["months_each"] == 11
    assert split["first_range"] == "2024-10..2025-08"
    assert split["second_range"] == "2025-10..2026-08"
    assert split["aligned"] is True
    assert split["dropped_months"] == ["2025-09"]


@pytest.mark.parametrize(
    ("months", "expected"),
    [
        pytest.param(12, False, id="12-mo-halves-not-aligned"),
        pytest.param(23, True, id="23-mo-default-window"),
        pytest.param(24, True, id="24-mo-whole-years"),
        pytest.param(35, False, id="35-mo"),
        pytest.param(36, False, id="36-mo-three-years"),
        pytest.param(44, False, id="44-mo"),
        pytest.param(47, True, id="47-mo"),
        pytest.param(48, True, id="48-mo-four-years"),
        pytest.param(72, True, id="72-mo-six-years"),
    ],
)
def test_alignment_follows_the_offset_rule(months, expected):
    """aligned iff the offset between halves (n - n//2) is a multiple of 12.

    Counter-intuitively 12 and 36 months are *not* usable while 23 and 24 are,
    which is why the default window is 24. Found by extending a live study to
    44 months and watching the confidence grade fall.
    """
    labels = common.month_range(common.shift_month("2026-08", -(months - 1)), "2026-08")

    split = analyze_mod.split_windows(labels)

    assert len(labels) == months
    assert split["available"] is True, months
    offset = months - months // 2
    assert split["aligned"] is expected, (
        f"{months} months -> offset {offset}, expected aligned={expected}"
    )


def test_too_short_window_has_no_growth_comparison():
    labels = common.month_range("2026-01", "2026-06")
    assert analyze_mod.split_windows(labels)["available"] is False


# ------------------------------------------------------------- seasonality ----
def test_seasonality_profile_needs_a_full_year():
    labels = common.month_range("2026-01", "2026-06")
    profile = analyze_mod.seasonality_profile(labels, [1] * 6)
    assert profile["available"] is False


def test_seasonality_profile_finds_the_peak_and_measures_dominance():
    labels = common.month_range("2024-01", "2025-12")  # exactly two cycles
    values = [400 if label.endswith("-03") else 100 for label in labels]

    profile = analyze_mod.seasonality_profile(labels, values)

    assert profile["available"] is True
    assert profile["reliable"] is True
    assert profile["cycles"] == 2
    assert profile["peak_month"] == 3
    assert profile["peak_mean"] == 400.0
    # 22 months at 100 + 2 at 400 = 3000 / 24 = 125 -> 400/125 = 3.2
    assert profile["peak_ratio"] == pytest.approx(3.2)


def test_seasonality_is_flagged_unreliable_under_two_cycles():
    labels = common.month_range("2024-10", "2026-08")  # 23 months
    profile = analyze_mod.seasonality_profile(labels, [10] * 23)
    assert profile["available"] is True
    assert profile["reliable"] is False


# ------------------------------------------------------------- grade table ----
GOOD = dict(
    months=24,
    aligned=True,
    direction_agrees=True,
    r2_share=0.8,
    strong_seasonality=False,
    possible_seasonality=False,
    mean_views=500.0,
)


def test_grade_high_when_everything_holds():
    level, reasons = analyze_mod.grade(**GOOD)
    assert level == "high"
    assert reasons


def test_grade_low_when_window_is_too_short():
    level, _ = analyze_mod.grade(**{**GOOD, "months": 11})
    assert level == "low"


def test_grade_low_when_absolute_and_normalised_disagree():
    """Disagreement means the move is wiki traffic, not topic interest."""
    level, reasons = analyze_mod.grade(**{**GOOD, "direction_agrees": False})
    assert level == "low"
    assert any("wiki traffic" in r for r in reasons)


def test_grade_low_when_seasonality_dominates_a_weak_fit():
    level, _ = analyze_mod.grade(
        **{**GOOD, "strong_seasonality": True, "r2_share": 0.2}
    )
    assert level == "low"


def test_grade_medium_for_reliable_seasonality_with_a_good_fit():
    level, _ = analyze_mod.grade(**{**GOOD, "strong_seasonality": True})
    assert level == "medium"


def test_grade_low_on_very_low_volume():
    level, reasons = analyze_mod.grade(**{**GOOD, "mean_views": 30.0})
    assert level == "low"
    assert any("views/month" in r for r in reasons)


def test_grade_medium_below_two_full_cycles():
    level, reasons = analyze_mod.grade(**{**GOOD, "months": 23})
    assert level == "medium"
    assert any("24 months" in r for r in reasons)


def test_grade_notes_possible_seasonality_without_claiming_it():
    level, reasons = analyze_mod.grade(
        **{**GOOD, "months": 23, "possible_seasonality": True}
    )
    assert level == "medium"
    assert any("not reliably estimated" in r for r in reasons)


def test_grade_downgrades_unaligned_halves():
    level, _ = analyze_mod.grade(**{**GOOD, "aligned": False})
    assert level == "medium"


# ------------------------------------------------------------- golden metrics -
def test_golden_trend_metrics(golden_analysis):
    metrics = golden_analysis["metrics"]

    pl = metrics["pl"]
    cs = metrics["cs"]

    assert pl["article_title"] == "Post"
    assert pl["article_total"] == 38_863
    assert pl["yoy"]["first_range"] == "2024-10..2025-08"
    assert pl["yoy"]["second_range"] == "2025-10..2026-08"
    assert pl["yoy"]["aligned"] is True
    assert pl["yoy"]["dropped_months"] == ["2025-09"]
    assert pl["yoy"]["article_first"] == 20_837
    assert pl["yoy"]["article_second"] == 17_239
    assert pl["yoy"]["article_pct"] == pytest.approx(-17.27, abs=0.05)
    assert pl["yoy"]["share_pct"] == pytest.approx(-8.65, abs=0.05)
    assert pl["share_ppm"] == pytest.approx(8.56, abs=0.01)
    assert pl["flags"]["direction_agrees"] is True

    assert cs["article_title"] == "Přerušovaný půst"
    assert cs["article_total"] == 6_546
    assert cs["yoy"]["article_first"] == 4_348
    assert cs["yoy"]["article_second"] == 1_928
    assert cs["yoy"]["article_pct"] == pytest.approx(-55.66, abs=0.05)
    assert cs["yoy"]["share_pct"] == pytest.approx(-49.05, abs=0.05)
    assert cs["share_ppm"] == pytest.approx(4.46, abs=0.01)
    assert cs["flags"]["direction_agrees"] is True


def test_golden_analysis_is_capped_at_medium_and_says_why(golden_analysis):
    """23 months is under two seasonal cycles, so 'high' must be impossible."""
    for language, metric in golden_analysis["metrics"].items():
        assert metric["confidence"] in {"medium", "low"}, language
        assert any("24 months" in r for r in metric["confidence_reasons"]), language


def test_comparison_ranks_polish_higher_on_share(golden_analysis):
    comparison = golden_analysis["comparison"]
    assert comparison["highest_share"] == "pl"
    # Both fall, so 'fastest growth' is the least-bad: still a decline.
    assert comparison["fastest_growth"] == "pl"
    assert [row["language"] for row in comparison["by_growth_share_pct"]] == [
        "pl",
        "cs",
    ]


def test_assumptions_and_limitations_are_always_present(golden_analysis):
    assert golden_analysis["assumptions"]
    assert any("not willingness to pay" in x for x in golden_analysis["limitations"])
    assert any("design parameters" in x for x in golden_analysis["assumptions"])


# ------------------------------------------------------------- gap reporting --
def test_gap_is_reported_and_never_substituted(gap_analysis):
    assert gap_analysis["gaps"] == ["pl"]
    assert "pl" not in gap_analysis["metrics"]
    assert any(
        "never filled with a substitute" in x for x in gap_analysis["limitations"]
    )
    assert "not measurable" in gap_analysis["headline"]


def test_substitute_is_declared_in_headline_and_limitations(golden_analysis):
    assert "Substitute in use" in golden_analysis["headline"]
    assert "no native article exists" in golden_analysis["headline"]
    substitution = [
        x for x in golden_analysis["limitations"] if "Manual substitution" in x
    ]
    assert substitution, "a chosen-for-you article must be disclosed"
    assert "not like-for-like" in substitution[0]


def test_seasonal_caveat_is_reported_even_when_unreliable(golden_analysis):
    seasonal = [x for x in golden_analysis["limitations"] if "Seasonal signal" in x]
    assert seasonal, "the Lent spike should be called out"
    assert "peak" in seasonal[0]


# -------------------------------------------------------------- headline ------
def test_headline_is_numeric_and_names_the_window(make_series):
    payload = make_series(
        labels=common.month_range("2024-10", "2026-08"),
        article=[100] * 23,
        project=[1_000_000] * 23,
        language="pl",
        title="Post",
    )
    analysis = analyze_mod.analyze(payload)
    assert "2024-10..2026-08" in analysis["headline"]
    assert "pl" in analysis["headline"]


def test_headline_reports_nothing_when_data_is_insufficient(make_series):
    payload = make_series(
        labels=common.month_range("2026-01", "2026-06"),
        article=[10] * 6,
        project=[1_000_000] * 6,
        language="pl",
        title="Post",
    )
    analysis = analyze_mod.analyze(payload)
    assert "No language edition has enough data" in analysis["headline"]


# ---------------------------------------------------------- data layers -----
def _layered(
    make_series,
    *,
    labels: list[str],
    article: list[int],
    project: list[int],
    language: str = "pl",
    title: str = "Post",
    access_views: dict[str, list[int]] | None = None,
    all_agents: list[int] | None = None,
    top_rank: dict | None = None,
    rank_by: str | None = None,
    success: list[dict[str, Any]] | None = None,
    second: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """A series payload carrying optional layer data (and a second language)."""
    payload = make_series(
        labels=labels,
        article=article,
        project=project,
        language=language,
        title=title,
    )
    entry = payload["series"][language]
    if access_views is not None:
        entry["access_views"] = access_views
    if all_agents is not None:
        entry["all_agents_views"] = all_agents
    if top_rank is not None:
        entry["top_rank"] = top_rank
    if second is not None:
        payload["series"][second["language"]] = second
    layers = [
        name
        for name, present in (
            ("access", access_views is not None),
            ("bot", all_agents is not None),
            ("top", top_rank is not None),
        )
        if present
    ]
    payload["parameters"]["layers"] = layers
    if rank_by:
        payload["parameters"]["rank_by"] = rank_by
    if success:
        payload["parameters"]["success"] = success
    return payload


def test_layer_metrics_are_derived_from_the_extra_series(make_series):
    """The layers answer questions the base series cannot, in one vocabulary.

    The device split partitions the base total; the bot share is
    ``all-agents - user`` over ``all-agents``; the top rank is carried
    through unchanged (its meaning belongs to the endpoint, not to us).
    """
    labels = common.month_range("2024-10", "2026-08")
    payload = _layered(
        make_series,
        labels=labels,
        article=[60] * len(labels),
        project=[1_000_000] * len(labels),
        access_views={
            "desktop": [10] * len(labels),
            "mobile-web": [30] * len(labels),
            "mobile-app": [20] * len(labels),
        },
        all_agents=[90] * len(labels),
        top_rank={"month": "2026-08", "rank": 42, "list_size": 1000},
    )

    metric = analyze_mod.analyze(payload)["metrics"]["pl"]

    split = metric["access_split"]
    assert split["total_views"] == 60 * len(labels)
    assert split["desktop_pct"] == 16.67
    assert split["mobile_pct"] == 83.33
    # 90 * 23 all-agents, of which 60 * 23 is the user series.
    assert metric["bot_share_pct"] == 33.33
    assert metric["top_rank"] == {"month": "2026-08", "rank": 42, "list_size": 1000}


def test_without_layers_no_layer_metric_appears(golden_analysis):
    """The base analysis stays exactly as it was: no phantom columns."""
    metric = golden_analysis["metrics"]["cs"]
    assert "access_split" not in metric
    assert "bot_share_pct" not in metric
    assert "top_rank" not in metric
    assert "layers" not in golden_analysis["parameters"]
    assert "ranked_by" not in golden_analysis["comparison"]


def test_ranked_by_orders_best_first_and_ascending_for_bots(make_series):
    """``bot_share`` is the one criterion where the *lowest* number wins.

    A report that ranked pollution first would lead its reader to the
    noisiest audience, which is the opposite of what the question asked.
    """
    labels = common.month_range("2024-10", "2026-08")
    payload = _layered(
        make_series,
        labels=labels,
        article=[100] * len(labels),
        project=[1_000_000] * len(labels),
        all_agents=[100] * len(labels),  # pl: 0% bot traffic
        rank_by="bot_share",
        second={
            "language": "cs",
            "project": "cs.wikipedia.org",
            "article_title": "Přerušovaný půst",
            "resolved_via": "sitelink",
            "native_gap": False,
            "labels": labels,
            "article_views": [100] * len(labels),
            "project_views": [1_000_000] * len(labels),
            "all_agents_views": [200] * len(labels),  # cs: 50% bot traffic
        },
    )

    ranked = analyze_mod.analyze(payload)["comparison"]["ranked_by"]

    assert ranked["criterion"] == "bot_share"
    assert ranked["order"] == ["pl", "cs"]


def test_ranked_by_for_share_is_descending(make_series):
    """The default criteria keep their meaning: more reading, higher rank."""
    labels = common.month_range("2024-10", "2026-08")
    payload = _layered(
        make_series,
        labels=labels,
        article=[100] * len(labels),
        project=[10_000_000] * len(labels),
        rank_by="share_ppm",
        second={
            "language": "cs",
            "project": "cs.wikipedia.org",
            "article_title": "Půst",
            "resolved_via": "sitelink",
            "native_gap": False,
            "labels": labels,
            "article_views": [100] * len(labels),
            "project_views": [1_000_000] * len(labels),
        },
    )

    ranked = analyze_mod.analyze(payload)["comparison"]["ranked_by"]
    # pl: 10 ppm, cs: 100 ppm -- the richer edition leads.
    assert ranked["order"] == ["cs", "pl"]


def test_a_criterion_without_data_ranks_no_one(make_series):
    """A ranking over unmeasured metrics is empty, never invented.

    Validation refuses this combination at the manifest, so reaching it
    means the data was lost *after* validation; an empty order says so
    instead of silently falling back to some other ranking.
    """
    labels = common.month_range("2024-10", "2026-08")
    payload = _layered(
        make_series,
        labels=labels,
        article=[100] * len(labels),
        project=[1_000_000] * len(labels),
        rank_by="mobile_share",  # declared, but no access layer was fetched
    )

    analysis = analyze_mod.analyze(payload)

    assert analysis["comparison"]["ranked_by"]["order"] == []


def test_layer_limitations_are_added_only_for_measured_layers(
    golden_analysis, make_series
):
    """Every limitation the report shows must describe a metric it shows."""
    golden = " ".join(golden_analysis["limitations"])
    assert "Device split" not in golden
    assert "Bot share" not in golden
    assert "Top rank" not in golden

    labels = common.month_range("2024-10", "2026-08")
    all_layers = _layered(
        make_series,
        labels=labels,
        article=[60] * len(labels),
        project=[1_000_000] * len(labels),
        access_views={
            "desktop": [10] * len(labels),
            "mobile-web": [30] * len(labels),
            "mobile-app": [20] * len(labels),
        },
        all_agents=[90] * len(labels),
        top_rank={"month": "2026-08", "rank": None, "list_size": 1000},
    )
    everything = " ".join(analyze_mod.analyze(all_layers)["limitations"])
    # All three measured at once cost ONE line: three bullets pushed a real
    # one-page PDF over the edge (see tests/test_report.py).
    assert everything.count("measured here") == 1
    assert "Device split" in everything and "top rank" in everything

    # A partial set keeps its own wording -- and never claims what was not run.
    access_only = _layered(
        make_series,
        labels=labels,
        article=[60] * len(labels),
        project=[1_000_000] * len(labels),
        access_views={
            "desktop": [10] * len(labels),
            "mobile-web": [30] * len(labels),
            "mobile-app": [20] * len(labels),
        },
    )
    partial = " ".join(analyze_mod.analyze(access_only)["limitations"])
    assert "Device split measured here" in partial
    assert "Bot share" not in partial
    assert "Top rank" not in partial


# ------------------------------------------------------- success criteria ---
def test_success_rules_are_graded_per_language(make_series):
    """The verdict is the rule applied to the data -- pass, fail, and n/m."""
    labels = common.month_range("2024-10", "2026-08")
    payload = make_series(
        labels=labels,
        article=[100] * len(labels),
        project=[1_000_000] * len(labels),
    )
    payload["parameters"]["success"] = [
        {"id": "volume", "metric": "share_ppm", "op": ">=", "value": 50},
        {"id": "growing", "metric": "yoy_share_pct", "op": ">", "value": 0},
        {"id": "visible", "metric": "mean_monthly_views", "op": "<", "value": 1000},
    ]

    analysis = analyze_mod.analyze(payload)
    verdicts = analysis["criteria"]["verdicts"]["xx"]

    # 100 views / 1,000,000 project views = 100 ppm; flat series = 0% growth.
    assert verdicts["volume"] == {"value": 100.0, "passed": True}
    assert verdicts["growing"]["passed"] is False
    assert verdicts["visible"]["passed"] is True
    assert analysis["criteria"]["summary"]["xx"] == {
        "met": 2,
        "total": 3,
        "not_evaluable": 0,
    }
    # The rules travel into analysis.json verbatim -- the agent's own words.
    assert analysis["criteria"]["success"][0]["id"] == "volume"


def test_an_unmeasurable_criterion_is_null_never_false(make_series):
    """A rule on data that was not fetched is unmeasurable, not failed."""
    labels = common.month_range("2024-10", "2026-08")
    payload = _layered(
        make_series,
        labels=labels,
        article=[100] * len(labels),
        project=[1_000_000] * len(labels),
        top_rank={"month": "2026-08", "rank": None, "list_size": 1000},
        success=[
            {"id": "bots", "metric": "bot_share_pct", "op": "<", "value": 20},
            {"id": "listed", "metric": "top_rank", "op": "<=", "value": 100},
        ],
    )

    verdicts = analyze_mod.analyze(payload)["criteria"]["verdicts"]["pl"]

    assert verdicts["bots"] == {
        "value": None,
        "passed": None,
        "reason": "layer not measured",
    }
    assert verdicts["listed"] == {
        "value": None,
        "passed": None,
        "reason": "outside the top list",
    }


def test_a_coverage_gap_reports_every_rule_as_not_measurable(make_series):
    """A language with no article cannot fail a criterion about that article."""
    labels = common.month_range("2024-10", "2026-08")
    payload = make_series(
        labels=labels,
        article=[100] * len(labels),
        project=[1_000_000] * len(labels),
        gaps=["yy"],
    )
    payload["parameters"]["success"] = [
        {"id": "volume", "metric": "share_ppm", "op": ">=", "value": 50},
    ]

    analysis = analyze_mod.analyze(payload)

    assert analysis["criteria"]["verdicts"]["yy"]["volume"] == {
        "value": None,
        "passed": None,
        "reason": "no article",
    }
    assert analysis["criteria"]["summary"]["yy"] == {
        "met": 0,
        "total": 1,
        "not_evaluable": 1,
    }
    # The measured language still gets its real verdict beside the gap.
    assert analysis["criteria"]["verdicts"]["xx"]["volume"]["passed"] is True


def test_growth_unavailable_is_reported_as_such(make_series):
    """Too short for halves -> the growth rule says so instead of guessing."""
    labels = common.month_range("2026-01", "2026-06")
    payload = make_series(
        labels=labels,
        article=[50] * len(labels),
        project=[1_000_000] * len(labels),
    )
    payload["parameters"]["success"] = [
        {"id": "growing", "metric": "yoy_share_pct", "op": ">=", "value": -5},
    ]

    verdict = analyze_mod.analyze(payload)["criteria"]["verdicts"]["xx"]["growing"]

    assert verdict["passed"] is None
    assert verdict["reason"] == "growth unavailable"


def test_confidence_compares_on_its_ordinal_scale(make_series):
    """``medium >= low`` is a grade comparison, not a string one."""
    labels = common.month_range("2024-10", "2026-08")
    payload = make_series(
        labels=labels,
        article=[100] * len(labels),
        project=[1_000_000] * len(labels),
    )
    payload["parameters"]["success"] = [
        {"id": "elite", "metric": "confidence", "op": ">=", "value": "high"},
        {"id": "any_grade", "metric": "confidence", "op": ">=", "value": "low"},
    ]

    analysis = analyze_mod.analyze(payload)
    grade = analysis["metrics"]["xx"]["confidence"]
    verdicts = analysis["criteria"]["verdicts"]["xx"]

    assert verdicts["any_grade"]["passed"] is True  # every grade >= low
    assert verdicts["elite"]["passed"] is (grade == "high")


def test_a_study_without_success_rules_gains_no_new_key(golden_analysis):
    """Byte-identity: criteria are opt-in, and absence stays absence."""
    assert "criteria" not in golden_analysis
