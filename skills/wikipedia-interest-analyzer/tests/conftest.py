"""Shared test setup: offline-only fixtures and reusable study builders.

Every test in this suite runs **without network access**. ``offline`` monkey
patches ``common.get_json`` so that only URLs present in
``tests/fixtures/manifest.json`` resolve; anything else raises, which turns an
unrecorded dependency into a loud failure instead of a live request.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Callable

import pytest

from helpers import GOLDEN_SINCE, GOLDEN_TODAY, GOLDEN_UNTIL, load_fixture

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"

if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import common  # noqa: E402

common.configure_console()


@pytest.fixture(autouse=True)
def offline(monkeypatch: pytest.MonkeyPatch) -> Callable[[str], Any]:
    """Replace the HTTP layer with fixture lookups (no network possible).

    Autouse, so the offline guarantee is structural: a test cannot forget to
    ask for it, and an unrecorded URL raises instead of hitting the network.
    """
    monkeypatch.setattr(common, "get_json", load_fixture)
    return load_fixture


@pytest.fixture
def study_factory() -> Callable[..., dict[str, Any]]:
    """Build a study manifest in memory (as ``run.py init`` would)."""

    def _make(
        *,
        topic: str = "intermittent fasting",
        languages: list[str] | None = None,
        overrides: dict[str, str] | None = None,
        window: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        return {
            "version": 1,
            "topic": topic,
            "languages": languages or ["pl", "cs"],
            "window": window
            if window is not None
            else {"since": GOLDEN_SINCE, "until": GOLDEN_UNTIL},
            "access": common.DEFAULT_ACCESS,
            "agent": common.DEFAULT_AGENT,
            "granularity": "monthly",
            "overrides": overrides or {},
            "resolution": None,
        }

    return _make


@pytest.fixture
def make_series() -> Callable[..., dict[str, Any]]:
    """Build a minimal ``series.json``-shaped payload for isolated tests."""

    def _make(
        labels: list[str],
        article: list[int],
        project: list[int],
        *,
        language: str = "xx",
        title: str = "Example",
        resolved_via: str = "sitelink",
        native_gap: bool = False,
        gaps: list[str] | None = None,
    ) -> dict[str, Any]:
        if not len(labels) == len(article) == len(project):
            raise ValueError(
                "labels/article/project must be the same length, got "
                f"{len(labels)}/{len(article)}/{len(project)}"
            )
        return {
            "generated_at": "2026-09-24",
            "topic": "example topic",
            "qid": "Q0",
            "window": {
                "since": labels[0],
                "until": labels[-1],
                "months": len(labels),
            },
            "parameters": {
                "access": "all-access",
                "agent": "user",
                "granularity": "monthly",
            },
            "series": {
                language: {
                    "language": language,
                    "project": f"{language}.wikipedia.org",
                    "article_title": title,
                    "resolved_via": resolved_via,
                    "native_gap": native_gap,
                    "labels": labels,
                    "article_views": article,
                    "project_views": project,
                }
            },
            "gaps": gaps or [],
            "warnings": [],
        }

    return _make


@pytest.fixture
def resolve_study(study_factory: Callable[..., dict[str, Any]], offline) -> Callable[[], dict[str, Any]]:
    """A study with its resolution filled in, using recorded responses."""

    def _resolve(**kwargs: Any) -> dict[str, Any]:
        import resolve as resolve_mod

        study = study_factory(**kwargs)
        study["resolution"] = resolve_mod.resolve(
            study["topic"], study["languages"], search_language="en"
        )
        return study

    return _resolve


@pytest.fixture
def golden_analysis(resolve_study: Callable[..., dict[str, Any]]) -> dict[str, Any]:
    """Full offline pipeline (resolve -> fetch -> analyze) on the golden study."""
    import analyze as analyze_mod
    import fetch as fetch_mod

    study = resolve_study(overrides={"pl": "Post"})
    series = fetch_mod.fetch_series(study, today=GOLDEN_TODAY)
    return analyze_mod.analyze(series)


@pytest.fixture
def gap_analysis(resolve_study: Callable[..., dict[str, Any]]) -> dict[str, Any]:
    """Same pipeline with *no* override, so Polish stays a coverage gap."""
    import analyze as analyze_mod
    import fetch as fetch_mod

    study = resolve_study()
    series = fetch_mod.fetch_series(study, today=GOLDEN_TODAY)
    return analyze_mod.analyze(series)
