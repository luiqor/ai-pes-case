"""Analysis: golden trend metrics, statistics primitives, and the grade table.

The golden numbers were verified by hand against production data before being
written down here; if any of them drift, the maths or the window handling has
regressed.
"""

from __future__ import annotations

import pytest

import analyze as analyze_mod
import common


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
    labels = common.month_range(
        common.shift_month("2026-08", -(months - 1)), "2026-08"
    )

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
    assert [row["language"] for row in comparison["by_growth_share_pct"]] == ["pl", "cs"]


def test_assumptions_and_limitations_are_always_present(golden_analysis):
    assert golden_analysis["assumptions"]
    assert any("not willingness to pay" in x for x in golden_analysis["limitations"])
    assert any("design parameters" in x for x in golden_analysis["assumptions"])


# ------------------------------------------------------------- gap reporting --
def test_gap_is_reported_and_never_substituted(gap_analysis):
    assert gap_analysis["gaps"] == ["pl"]
    assert "pl" not in gap_analysis["metrics"]
    assert any("never filled with a substitute" in x for x in gap_analysis["limitations"])
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
