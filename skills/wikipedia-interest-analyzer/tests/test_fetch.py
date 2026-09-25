"""Fetching: window resolution, zero gap-filling, overshoot, and gap policy.

The golden assertions below were verified against production pageview data
(hand-checked totals for Polish ``Post`` and Czech ``Přerušovaný půst`` over
2024-10..2026-08). They are the numbers that prove the pipeline is correct --
in particular the "request one extra month" rule, without which the final month
of every study is silently truncated.
"""

from __future__ import annotations

from datetime import date

import common
import fetch as fetch_mod
import pytest
from helpers import GOLDEN_TODAY, GOLDEN_UNTIL, load_fixture


# ------------------------------------------------------------- golden series -
def test_golden_series_totals(resolve_study):
    study = resolve_study(overrides={"pl": "Post"})
    payload = fetch_mod.fetch_series(study, today=GOLDEN_TODAY)

    assert payload["window"] == {
        "since": "2024-10",
        "until": "2026-08",
        "months": 23,
    }
    pl = payload["series"]["pl"]
    cs = payload["series"]["cs"]

    assert pl["article_title"] == "Post"
    assert cs["article_title"] == "Přerušovaný půst"
    assert len(pl["labels"]) == 23
    assert pl["labels"][0] == "2024-10"
    assert pl["labels"][-1] == "2026-08"

    # Hand-verified totals for this window.
    assert sum(pl["article_views"]) == 38_863
    assert sum(cs["article_views"]) == 6_546


def test_the_final_month_is_not_truncated(resolve_study):
    """Regression test for the discovered per-article end-bucket truncation.

    ``2026-08`` must hold the complete month (119 views for the Czech article).
    Requesting the window's end directly yields 2 instead -- a single day --
    which is why fetch.py overshoots by one month and discards it.
    """
    study = resolve_study(overrides={"pl": "Post"})
    payload = fetch_mod.fetch_series(study, today=GOLDEN_TODAY)
    cs = payload["series"]["cs"]

    august = cs["article_views"][cs["labels"].index("2026-08")]
    assert august == 119

    # The overshoot month itself must never leak into the window.
    assert "2026-09" not in cs["labels"]
    assert payload["window"]["until"] == GOLDEN_UNTIL


def test_series_are_aligned_per_language(resolve_study):
    payload = fetch_mod.fetch_series(
        resolve_study(overrides={"pl": "Post"}), today=GOLDEN_TODAY
    )
    for item in payload["series"].values():
        assert len(item["labels"]) == len(item["article_views"])
        assert len(item["labels"]) == len(item["project_views"])
        assert all(v >= 0 for v in item["article_views"])


# ------------------------------------------------------------- gap handling --
def test_no_override_leaves_the_gap_intact(resolve_study):
    payload = fetch_mod.fetch_series(resolve_study(), today=GOLDEN_TODAY)

    assert payload["gaps"] == ["pl"]
    assert "pl" not in payload["series"]
    assert any("coverage gap" in w for w in payload["warnings"])


def test_override_marks_the_native_gap_so_it_stays_visible(resolve_study):
    payload = fetch_mod.fetch_series(
        resolve_study(overrides={"pl": "Post"}), today=GOLDEN_TODAY
    )
    # The override gives us data, but it must not erase the fact that Polish has
    # no article of its own for this topic.
    assert payload["gaps"] == []
    assert payload["series"]["pl"]["resolved_via"] == "override"
    assert payload["series"]["pl"]["native_gap"] is True
    assert payload["series"]["cs"]["native_gap"] is False


def test_misspelled_override_fails_instead_of_reporting_zero_interest(resolve_study):
    """A typo in a manual override must never look like 'nobody cares'."""
    study = resolve_study(overrides={"pl": "Głódówka lecznicza"})
    with pytest.raises(SystemExit) as excinfo:
        fetch_mod.fetch_series(study, today=GOLDEN_TODAY)
    assert "does not exist" in str(excinfo.value)


# ------------------------------------------------------------ window rules ----
def test_window_is_clamped_to_complete_months(study_factory):
    study = study_factory(
        overrides={"pl": "Post"},
        window={"since": "2024-10", "until": "2026-12"},  # future end
    )
    since, until, warnings = fetch_mod.resolve_window(study, date(2026, 9, 24))
    assert until == "2026-08"
    assert any("not a complete month" in w for w in warnings)


def test_window_is_clamped_to_the_pageviews_era(study_factory):
    study = study_factory(
        overrides={"pl": "Post"}, window={"since": "2009-01", "until": "2016-01"}
    )
    since, until, warnings = fetch_mod.resolve_window(study, date(2026, 9, 24))
    assert since == common.DATA_START_MONTH
    assert until == "2016-01"
    assert any("pageview data starts" in w for w in warnings)


def test_window_ending_before_the_pageviews_era_is_rejected(study_factory):
    study = study_factory(window={"since": "2009-01", "until": "2010-01"})
    with pytest.raises(SystemExit) as excinfo:
        fetch_mod.resolve_window(study, date(2026, 9, 24))
    assert "no complete months" in str(excinfo.value)


def test_default_window_is_last_24_complete_months(study_factory):
    study = study_factory(window={}, overrides={"pl": "Post"})
    since, until, _ = fetch_mod.resolve_window(study, date(2026, 9, 24))
    assert (since, until) == ("2024-09", "2026-08")
    assert len(common.month_range(since, until)) == 24


def test_monthly_granularity_is_enforced(tmp_path, study_factory):
    study = study_factory()
    study["granularity"] = "daily"
    path = tmp_path / "study.json"
    common.write_json(path, study)
    with pytest.raises(SystemExit) as excinfo:
        fetch_mod.load_study(path)
    assert "monthly" in str(excinfo.value)


def test_missing_manifest_explains_how_to_create_one(tmp_path):
    with pytest.raises(SystemExit) as excinfo:
        fetch_mod.load_study(tmp_path / "nope.json")
    assert "run.py init" in str(excinfo.value)


def test_unalignable_window_is_flagged_before_any_fetch(study_factory):
    """Extending a window must not silently cap the confidence grade."""
    study = study_factory(window={"since": "2023-01", "until": "2026-08"})  # 44 mo
    _, _, warnings = fetch_mod.resolve_window(study, date(2026, 9, 24))
    assert any("capped at medium" in w for w in warnings), warnings
    assert any("24 months" in w for w in warnings), "the fix must be actionable"


@pytest.mark.parametrize(
    "window",
    [
        pytest.param({}, id="default-24-months"),
        pytest.param({"since": "2023-10", "until": "2025-09"}, id="explicit-24-months"),
    ],
)
def test_alignable_windows_do_not_warn(window, study_factory):
    _, _, warnings = fetch_mod.resolve_window(
        study_factory(window=window), date(2026, 9, 24)
    )
    assert not any("capped at medium" in w for w in warnings), window


def test_effective_titles_precedence(resolve_study):
    study = resolve_study(overrides={"cs": "Půst"})
    titles, sources, gaps, native_missing, warnings = fetch_mod.effective_titles(study)

    # An explicit override always wins over the resolved sitelink...
    assert titles["cs"] == "Půst"
    assert sources["cs"] == "override"
    # ...and because cs *does* have a native article, this is not a gap fill.
    assert native_missing == []
    # Polish has neither an article nor an override, so it stays a gap.
    assert "pl" not in titles
    assert gaps == ["pl"]
    assert any("coverage gap" in w for w in warnings)


# ------------------------------------------------- response validation ------
def test_well_formed_points_become_month_keys():
    payload = {"items": [{"timestamp": "2024100100", "views": 7}]}
    assert fetch_mod._parse_points(payload, "https://example.test") == {"2024-10": 7}


def test_malformed_points_are_a_bad_body_not_a_traceback():
    """A corrupt body must classify as ``bad_body`` (SKILL.md maps kinds),
    instead of a bare KeyError/ValueError deep inside the fetch."""
    payload = {"items": [{"timestamp": "banana", "views": -1}]}
    with pytest.raises(common.ApiError) as excinfo:
        fetch_mod._parse_points(payload, "https://example.test")
    assert excinfo.value.kind == "bad_body"


def test_body_without_items_is_a_bad_body():
    with pytest.raises(common.ApiError) as excinfo:
        fetch_mod._parse_points({"unexpected": True}, "https://example.test")
    assert excinfo.value.kind == "bad_body"


def test_corrupt_pageview_response_stops_the_fetch_with_bad_body(resolve_study):
    """End to end: a corrupt *pageviews* body (Wikidata/Action API intact)
    fails the whole fetch loudly, quoting the URL's kind.

    The transport is injected rather than patched -- the seam the pipeline
    was designed around.
    """
    study = resolve_study()

    class CorruptPageviews:
        def get_json(self, url: str):
            if "/metrics/pageviews/" in url:
                return {"items": [{"timestamp": "oops", "views": "NaN"}]}
            return load_fixture(url)

    with pytest.raises(SystemExit) as excinfo:
        fetch_mod.fetch_series(study, today=GOLDEN_TODAY, client=CorruptPageviews())
    assert "error [bad_body]" in str(excinfo.value)
