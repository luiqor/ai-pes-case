"""Unit tests for the shared HTTP/URL layer.

These pin the parts of the Wikimedia API contract that were verified against
production: the exact URL shapes, the percent-encoding of non-ASCII titles, the
two distinct 404 bodies, and the fact that ``query.pages`` has been observed in
both object and list form.
"""

from __future__ import annotations

import json
from datetime import date

import common
import http_client
import pytest
from helpers import FIXTURES, MANIFEST


# ---------------------------------------------------------------- encoding ---
def test_encode_title_is_the_single_encoder():
    # Verified byte-for-byte against a live response.
    assert (
        common.encode_title("Přerušovaný půst")
        == "P%C5%99eru%C5%A1ovan%C3%BD_p%C5%AFst"
    )


@pytest.mark.parametrize(
    ("raw", "encoded"),
    [
        pytest.param("  Foo Bar  ", "Foo_Bar", id="trims-and-spaces-to-underscore"),
        pytest.param("AC/DC", "AC%2FDC", id="slash-is-escaped"),
        pytest.param("already_ok", "already_ok", id="idempotent-on-encoded-title"),
    ],
)
def test_encode_title_normalises_spaces_and_handles_reserved_characters(raw, encoded):
    assert common.encode_title(raw) == encoded


def test_per_article_url_matches_a_recorded_production_url():
    url = common.per_article_url(
        "cs.wikipedia.org", "Přerušovaný půst", "2024-10", "2026-09"
    )
    assert url in MANIFEST, "the encoder must reproduce the URL production used"


def test_aggregate_url_matches_the_verified_production_route():
    assert common.aggregate_url("pl.wikipedia.org", "2024-10", "2026-09").endswith(
        "/pageviews/aggregate/pl.wikipedia.org/all-access/user/monthly/"
        "2024100100/2026090100"
    )


def test_hourly_is_rejected_for_per_article():
    # Verified live: hourly on the per-article route returns 404 "invalid route".
    with pytest.raises(ValueError):
        common.per_article_url(
            "en.wikipedia.org", "x", "2024-10", "2024-11", granularity="hourly"
        )


# ------------------------------------------------------------ month helpers --
def test_month_to_stamp_renders_the_api_timestamp():
    assert common.month_to_stamp("2024-10") == "2024100100"


def test_month_to_stamp_rejects_a_non_iso_month():
    with pytest.raises(ValueError):
        common.month_to_stamp("2024/10")


def test_month_range_is_inclusive_and_ordered():
    assert common.month_range("2024-11", "2025-02") == [
        "2024-11",
        "2024-12",
        "2025-01",
        "2025-02",
    ]
    assert common.month_range("2026-08", "2026-08") == ["2026-08"]


@pytest.mark.parametrize(
    ("since", "until"),
    [
        pytest.param("2025-01", "2024-01", id="reversed-range"),
        pytest.param("2024-13", "2025-01", id="invalid-month-number"),
        pytest.param("garbage", "2025-01", id="not-a-month-at-all"),
    ],
)
def test_month_range_rejects_reversed_or_malformed_input(since, until):
    with pytest.raises(ValueError):
        common.month_range(since, until)


@pytest.mark.parametrize(
    ("month", "delta", "expected"),
    [
        pytest.param("2024-01", -1, "2023-12", id="backwards-across-year"),
        pytest.param("2024-12", 1, "2025-01", id="forwards-across-year"),
        pytest.param("2026-08", -23, "2024-09", id="twenty-three-months-back"),
    ],
)
def test_shift_month_crosses_year_boundaries(month, delta, expected):
    assert common.shift_month(month, delta) == expected


@pytest.mark.parametrize(
    ("today", "expected"),
    [
        pytest.param(date(2026, 9, 24), "2026-08", id="mid-month"),
        pytest.param(date(2026, 9, 1), "2026-08", id="first-of-month-still-partial"),
        pytest.param(date(2026, 3, 15), "2026-02", id="march-rolls-back-to-february"),
    ],
)
def test_last_complete_month_never_returns_the_current_month(today, expected):
    # The current month is always partial, so it can never be a window end.
    assert common.last_complete_month(today) == expected


def test_default_window_covers_the_requested_span():
    since, until = common.default_window(24, date(2026, 9, 24))
    assert (since, until) == ("2024-09", "2026-08")
    assert len(common.month_range(since, until)) == 24


def test_clamp_to_data_start_floors_at_the_2015_data_start():
    # Verified: pageview data starts 2015-07-01.
    assert common.clamp_to_data_start("2009-01") == "2015-07"
    assert common.clamp_to_data_start("2020-01") == "2020-01"


def test_project_for_builds_the_wikipedia_host():
    assert common.project_for("pl") == "pl.wikipedia.org"


def test_project_for_rejects_a_malformed_language_code():
    with pytest.raises(ValueError):
        common.project_for("NOT A LANG")


# ------------------------------------------------------- error classification -
@pytest.mark.parametrize(
    ("detail", "expected"),
    [
        pytest.param(
            {"detail": "invalid route"}, "invalid_route", id="invalid-route-body"
        ),
        pytest.param(
            {
                "detail": "Invalid or missing article parameter; ... we either do not "
                "have data for those date(s) and articles, or ... not loaded yet ..."
            },
            "no_data",
            id="no-data-body",
        ),
        pytest.param(
            {"detail": "something else"}, "not_found", id="unrecognised-body-falls-back"
        ),
    ],
)
def test_404_classification_separates_the_two_observed_bodies_and_falls_back(
    detail, expected
):
    assert common._classify(404, detail) == expected


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        pytest.param(429, "rate_limited", id="too-many-requests"),
        pytest.param(503, "server", id="service-unavailable"),
        pytest.param(400, "other", id="generic-client-error"),
    ],
)
def test_status_classification(status, expected):
    assert common._classify(status, "") == expected


# --------------------------------------------------------------- page parsing -
def test_iter_pages_handles_both_observed_shapes():
    as_object = {"query": {"pages": {"123": {"title": "A", "pageid": 123}}}}
    as_list = {"query": {"pages": [{"title": "A", "pageid": 123}]}}
    assert len(common.iter_pages(as_object)) == 1
    assert len(common.iter_pages(as_list)) == 1
    assert common.iter_pages({}) == []


EXISTING_PAGE = {
    "query": {"pages": {"74017": {"title": "Głodówka lecznicza", "pageid": 74017}}}
}
MISSING_PAGE = {
    "query": {"pages": {"-1": {"title": "Głódówka lecznicza", "missing": ""}}}
}


@pytest.mark.parametrize(
    ("payload", "title", "expected"),
    [
        pytest.param(
            EXISTING_PAGE, "Głodówka lecznicza", (True, 74017), id="existing-page"
        ),
        # Underscores (as the API echoes titles back) must match too.
        pytest.param(
            EXISTING_PAGE, "Głodówka_lecznicza", (True, 74017), id="underscored-title"
        ),
        pytest.param(
            MISSING_PAGE, "Głódówka lecznicza", (False, None), id="missing-page"
        ),
        pytest.param({}, "anything", (False, None), id="empty-payload"),
    ],
)
def test_page_exists_accepts_existing_and_rejects_missing(payload, title, expected):
    assert common.page_exists(payload, title) == expected


def test_recorded_misspelled_title_is_reported_as_missing():
    """A visually plausible misspelling must NOT be treated as an existing page.

    Recorded live: ``Głodówka`` (correct) exists, while ``Głódówka`` -- an easy
    to make transcription error -- returns ``missing``. This is exactly why
    titles are never hand-typed and always confirmed first.
    """
    payload = load_by_fragment("titles=G%C5%82%C3%B3d%C3%B3wka")
    assert common.page_exists(payload, "Głódówka lecznicza") == (False, None)


def load_by_fragment(fragment: str):
    """Fetch a recorded response whose URL contains *fragment* (test data only)."""
    for url, name in MANIFEST.items():
        if fragment in url:
            return json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    raise LookupError(f"no recorded URL containing {fragment!r}")


def test_strip_html_strips_tags_and_treats_none_as_empty():
    assert common.strip_html('<span class="searchmatch">foo</span> bar') == "foo bar"
    assert common.strip_html(None) == ""


# ------------------------------------------------------------ cache honesty --
# The cache lives on the client, so these tests build one per tmp dir instead
# of patching module globals: two clients cannot share state by accident.
def test_cache_roundtrip_is_silent_when_the_entry_is_usable(tmp_path, capsys):
    client = http_client.WikiClient(cache_dir=tmp_path)
    url = "https://example.test/metrics/roundtrip"
    client._write_cache(url, {"items": [1, 2]})

    assert client._read_cache(url) == {"items": [1, 2]}
    assert capsys.readouterr().err == "", "a healthy cache must stay quiet"


def test_missing_cache_entry_is_a_normal_cold_start_and_stays_silent(tmp_path, capsys):
    client = http_client.WikiClient(cache_dir=tmp_path)

    assert client._read_cache("https://example.test/metrics/never-seen") is (
        http_client._CACHE_MISS
    )
    assert capsys.readouterr().err == "", "first fetch is not a warning"


def test_unreadable_cache_entry_is_reported_before_being_discarded(tmp_path, capsys):
    """Discarding data must never be invisible: the run loses its reuse value."""
    client = http_client.WikiClient(cache_dir=tmp_path)
    url = "https://example.test/metrics/corrupt"
    entry = client._cache_path(url)
    entry.write_text("{truncated", encoding="utf-8")

    assert client._read_cache(url) is http_client._CACHE_MISS

    err = capsys.readouterr().err
    assert "unreadable cache entry" in err and entry.name in err
    assert not entry.exists(), "the corrupt entry must not be read again"


def test_cache_write_failure_is_reported_and_never_breaks_the_run(tmp_path, capsys):
    """A read-only cache degrades loudly, not silently, and never aborts."""
    blocker = tmp_path / "not-a-directory.txt"
    blocker.write_text("x", encoding="utf-8")
    client = http_client.WikiClient(cache_dir=blocker)

    client._write_cache("https://example.test/metrics/x", {"ok": 1})  # must not raise

    assert "could not write the response cache" in capsys.readouterr().err


# ------------------------------------------------------------ environment ---
# The two WIA_* variables are read once at import; the resolvers are called
# directly here so each failure mode is exercised without reloading common.
def test_user_agent_env_default_and_verbatim_passthrough(monkeypatch):
    monkeypatch.delenv("WIA_USER_AGENT", raising=False)
    assert "wikipedia-interest-analyzer" in common._user_agent_from_env()

    monkeypatch.setenv("WIA_USER_AGENT", "me@example.org")
    assert common._user_agent_from_env() == "me@example.org"


def test_user_agent_env_rejects_empty_and_multiline_values(monkeypatch):
    monkeypatch.setenv("WIA_USER_AGENT", "   ")
    with pytest.raises(SystemExit) as excinfo:
        common._user_agent_from_env()
    assert "empty" in str(excinfo.value)

    monkeypatch.setenv("WIA_USER_AGENT", "me@example.org\nX-Injected: 1")
    with pytest.raises(SystemExit) as excinfo:
        common._user_agent_from_env()
    assert "line breaks" in str(excinfo.value)


def test_cache_dir_env_empty_falls_back_to_the_skill_folder(monkeypatch):
    monkeypatch.setenv("WIA_CACHE_DIR", "")
    assert common._cache_dir_from_env() == common.SKILL_ROOT / "cache"


def test_cache_dir_env_rejects_a_path_that_is_a_file(monkeypatch, tmp_path):
    blocker = tmp_path / "not-a-directory.txt"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setenv("WIA_CACHE_DIR", str(blocker))
    with pytest.raises(SystemExit) as excinfo:
        common._cache_dir_from_env()
    assert "not a directory" in str(excinfo.value)


# ---------------------------------------------------------- month parsing ---
def test_parse_month_accepts_real_months_and_rejects_junk():
    assert common.parse_month("2024-10") == "2024-10"
    for bad in ("banana", "2024-13", "2024/10", "202410", ""):
        with pytest.raises(ValueError):
            common.parse_month(bad)
