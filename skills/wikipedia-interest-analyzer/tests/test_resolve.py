"""Concept -> article resolution, including the coverage-gap policy.

The central rule under test: a language with no Wikipedia article for the topic
is reported as a **gap** with candidate articles for the user to choose from.
Nothing is ever substituted automatically.
"""

from __future__ import annotations

import argparse

import common
import http_client
import pytest
import resolve as resolve_mod


def test_resolves_the_golden_topic():
    result = resolve_mod.resolve("intermittent fasting", ["pl", "cs"])

    assert result["qid"] == "Q1666254"
    assert result["label"] == "intermittent fasting"
    assert result["articles"]["cs"]["title"] == "Přerušovaný půst"
    assert result["articles"]["cs"]["source"] == "sitelink"
    assert result["articles"]["pl"] is None
    assert result["gaps"] == ["pl"]


def test_gap_offers_candidates_but_never_a_substitution():
    result = resolve_mod.resolve("intermittent fasting", ["pl", "cs"])

    candidates = result["candidates"]["pl"]
    assert candidates, "a gap must offer candidates for the user to consider"
    # Candidates are data, not decisions: no article was assigned for pl.
    assert result["articles"]["pl"] is None
    # Every returned candidate was confirmed to exist (stale hits are dropped).
    for candidate in candidates:
        assert candidate["title"]
        assert candidate["pageid"] is not None


def test_search_hits_are_reported_so_the_user_can_re_pick():
    result = resolve_mod.resolve("intermittent fasting", ["pl", "cs"])
    ids = [hit["id"] for hit in result["search_hits"]]
    assert "Q1666254" in ids
    assert len(ids) > 1, "the runner-up Wikidata hits must stay visible"


def test_explicit_qid_skips_the_topic_search(monkeypatch):
    def explode(*args, **kwargs):  # pragma: no cover - must not run
        raise AssertionError("wbsearchentities must not be called with --qid")

    monkeypatch.setattr(resolve_mod, "_search_concept", explode)
    result = resolve_mod.resolve(
        "whatever the user typed", ["pl", "cs"], qid="Q1666254"
    )
    assert result["qid"] == "Q1666254"
    assert result["articles"]["cs"]["title"] == "Přerušovaný půst"


def test_stale_search_hits_are_dropped_with_a_warning(monkeypatch):
    """A search hit whose page no longer exists must not become a candidate."""
    monkeypatch.setattr(
        resolve_mod,
        "_search_wiki",
        lambda language, term, limit, **_: {
            "query": {
                "search": [
                    {"title": "Live article", "snippet": "ok"},
                    {"title": "Deleted article", "snippet": "stale"},
                ]
            }
        },
    )
    monkeypatch.setattr(
        resolve_mod,
        "confirm_titles",
        lambda language, titles, **_: {
            "query": {"pages": {"1": {"title": "Live article", "pageid": 1}}}
        },
    )

    warnings: list[str] = []
    # Both network functions above are patched, so the client is never used;
    # it is required by the signature all call sites share.
    candidates = resolve_mod._confirmed_candidates(
        "pl", "topic", 3, warnings, client=http_client.default_client()
    )

    assert [c["title"] for c in candidates] == ["Live article"]
    assert warnings and "no longer exists" in warnings[0]


def test_missing_concept_raises_a_typed_error(monkeypatch):
    monkeypatch.setattr(
        resolve_mod,
        "_search_concept",
        lambda topic, language, limit, **_: {"search": []},
    )
    with pytest.raises(common.ApiError) as excinfo:
        resolve_mod.resolve("zzz not a real topic", ["pl"])
    assert excinfo.value.kind == "not_found"


def test_parse_languages_rejects_junk():
    with pytest.raises(argparse.ArgumentTypeError):
        resolve_mod.parse_languages("")
    with pytest.raises(argparse.ArgumentTypeError):
        resolve_mod.parse_languages("pl,NOT A LANG")
    assert resolve_mod.parse_languages(" PL , Cs ") == ["pl", "cs"]
