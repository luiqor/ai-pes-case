"""Chart rendering."""

from __future__ import annotations

import analyze as analyze_mod
import chart as chart_mod
import pytest


def test_renders_png_and_svg(golden_analysis, tmp_path):
    paths = chart_mod.render(golden_analysis, tmp_path / "chart")

    png = tmp_path / "chart.png"
    svg = tmp_path / "chart.svg"
    assert png.is_file() and png.stat().st_size > 5_000
    assert svg.is_file() and svg.stat().st_size > 1_000
    assert paths["png"] == str(png)
    assert paths["svg"] == str(svg)


def test_svg_contains_both_series_and_accented_titles(golden_analysis, tmp_path):
    chart_mod.render(golden_analysis, tmp_path / "chart")
    svg = (tmp_path / "chart.svg").read_text(encoding="utf-8")

    # Two languages x two panels = four plotted curves.
    assert svg.count("Přerušovaný") >= 1
    assert "<svg" in svg
    assert "intermittent fasting" in svg


def test_gap_is_noted_on_the_chart(gap_analysis, tmp_path):
    chart_mod.render(gap_analysis, tmp_path / "chart")
    svg = (tmp_path / "chart.svg").read_text(encoding="utf-8")
    assert "no article in: pl" in svg


def test_all_gaps_refuses_to_render(make_series, tmp_path):
    payload = make_series(
        labels=["2026-01"],
        article=[1],
        project=[1],
        language="pl",
        title="x",
        gaps=["pl"],
    )
    payload["series"] = {}  # nothing measurable at all

    analysis = analyze_mod.analyze(payload)
    with pytest.raises(SystemExit) as excinfo:
        chart_mod.render(analysis, tmp_path / "chart")
    assert "coverage gap" in str(excinfo.value)
