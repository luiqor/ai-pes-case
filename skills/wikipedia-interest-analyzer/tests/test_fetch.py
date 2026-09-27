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


# ---------------------------------------------------------- data layers -----
def _channel(month_views: int, access: str) -> int:
    """One recorded month split across the access channels, exactly.

    Desktop takes the half, the two mobile channels split what is left, so
    ``desktop + mobile-web + mobile-app == month_views`` for every month --
    the partition property verified live on 2026-09-27.
    """
    if access == "desktop":
        return month_views // 2
    half_rest = month_views - month_views // 2
    if access == "mobile-web":
        return half_rest // 2
    return half_rest - half_rest // 2


class LayerClient:
    """Synthetic layer bodies served beside the recorded base fixtures.

    The access channels split each recorded month into desktop half / two
    mobile quarters, so the three channels sum to the base series exactly --
    the equality verified live on 2026-09-27. ``all-agents`` is exactly
    twice the recorded user series, so the bot share computes to 50%. The
    top list always places ``Post`` third and never lists the Czech title.
    """

    def __init__(self, *, top_body: object | None = None) -> None:
        self.urls: list[str] = []
        self.top_body = top_body

    def get_json(self, url: str):
        self.urls.append(url)
        if "/pageviews/top/" in url:
            if self.top_body is not None:
                return self.top_body
            return {
                "items": [
                    {
                        "articles": [
                            {"rank": 1, "article": "Alpha", "views": 900},
                            {"rank": 2, "article": "Beta", "views": 500},
                            {"rank": 3, "article": "Post", "views": 400},
                            {"rank": 4, "article": "Gamma", "views": 300},
                            {"rank": 5, "article": "Delta", "views": 200},
                        ]
                    }
                ]
            }
        if "/per-article/" in url:
            project, access, agent, title, granularity, start, end = url.split(
                "/metrics/pageviews/per-article/", 1
            )[1].split("/")
            if agent == "all-agents":
                base = load_fixture(url.replace("/all-agents/", "/user/"))
                return {
                    "items": [
                        {"timestamp": pt["timestamp"], "views": pt["views"] * 2}
                        for pt in base["items"]
                    ]
                }
            if access in ("desktop", "mobile-web", "mobile-app"):
                base = load_fixture(url.replace(f"/{access}/", "/all-access/", 1))
                points = [
                    {
                        "timestamp": pt["timestamp"],
                        "views": _channel(pt["views"], access),
                    }
                    for pt in base["items"]
                ]
                return {"items": points}
        return load_fixture(url)


def test_layers_add_the_extra_series_and_echo_the_selection(resolve_study):
    """Each requested layer lands in series.json, and the selection echoes.

    The analysis stage reads ``series.json`` alone, so ``layers`` and
    ``rank_by`` must travel in ``parameters`` -- the same reason the study's
    access/agent filters were already echoed there.
    """
    study = resolve_study(overrides={"pl": "Post"})
    study["layers"] = ["access", "bot", "top"]
    study["criteria"] = {"rank_by": "bot_share"}
    client = LayerClient()

    payload = fetch_mod.fetch_series(study, today=GOLDEN_TODAY, client=client)

    assert payload["parameters"]["layers"] == ["access", "bot", "top"]
    assert payload["parameters"]["rank_by"] == "bot_share"

    cs = payload["series"]["cs"]
    channels = cs["access_views"]
    assert set(channels) == {"desktop", "mobile-web", "mobile-app"}
    for values in channels.values():
        assert len(values) == len(cs["labels"])
    # The three channels sum to the base series, month by month: the split is
    # a partition of what the study already measured, not a new measurement.
    summed = [
        d + w + a
        for d, w, a in zip(
            channels["desktop"],
            channels["mobile-web"],
            channels["mobile-app"],
            strict=True,
        )
    ]
    assert summed == cs["article_views"]

    # all-agents is twice the user series, so half of it is non-user traffic.
    assert cs["all_agents_views"] == [v * 2 for v in cs["article_views"]]

    # The top list is fetched for the window's final month, per project.
    assert cs["top_rank"] == {"month": "2026-08", "rank": None, "list_size": 5}
    assert payload["series"]["pl"]["top_rank"] == {
        "month": "2026-08",
        "rank": 3,
        "list_size": 5,
    }
    assert any(
        "/pageviews/top/pl.wikipedia.org/all-access/2026/08/all-days" in url
        for url in client.urls
    )
    assert not payload["warnings"] or all(
        "top list" not in w for w in payload["warnings"]
    )


def test_without_layers_no_layer_keys_and_no_extra_requests(resolve_study):
    """A study that asked for nothing extra fetches nothing extra.

    The two paths must be indistinguishable in cost: no layer URL may be
    requested, no layer key may appear for a reader to wonder about.
    """
    study = resolve_study(overrides={"pl": "Post"})
    client = LayerClient()

    payload = fetch_mod.fetch_series(study, today=GOLDEN_TODAY, client=client)

    assert "layers" not in payload["parameters"]
    assert "rank_by" not in payload["parameters"]
    for entry in payload["series"].values():
        assert "access_views" not in entry
        assert "all_agents_views" not in entry
        assert "top_rank" not in entry
    assert not any("/pageviews/top/" in url for url in client.urls)
    assert not any("/all-agents/" in url for url in client.urls)
    assert not any(
        f"/per-article/{entry['project']}/{channel}/" in url
        for entry in payload["series"].values()
        for channel in ("desktop", "mobile-web", "mobile-app")
        for url in client.urls
    )


def test_a_top_list_not_loaded_yet_skips_the_metric_with_a_warning(resolve_study):
    """A month the top endpoint has not loaded is "not ranked this run".

    The base study is complete without it, so the run continues with a
    warning rather than failing over data that does not exist yet.
    """
    study = resolve_study(overrides={"pl": "Post"})
    study["layers"] = ["top"]

    class NotLoadedYet(LayerClient):
        def get_json(self, url: str):
            if "/pageviews/top/" in url:
                self.urls.append(url)
                raise common.ApiError(
                    "no top list for this month", url=url, kind="no_data"
                )
            return super().get_json(url)

    payload = fetch_mod.fetch_series(
        study, today=GOLDEN_TODAY, client=NotLoadedYet()
    )
    for entry in payload["series"].values():
        assert "top_rank" not in entry
    assert any("not loaded yet" in w for w in payload["warnings"])


def test_a_malformed_top_body_stops_the_fetch_loudly(resolve_study):
    """A shape change in the top endpoint is a loud failure, never a blank."""
    study = resolve_study(overrides={"pl": "Post"})
    study["layers"] = ["top"]

    with pytest.raises(SystemExit) as excinfo:
        fetch_mod.fetch_series(
            study,
            today=GOLDEN_TODAY,
            client=LayerClient(top_body={"items": [{"unexpected": True}]}),
        )
    assert "error [bad_body]" in str(excinfo.value)
