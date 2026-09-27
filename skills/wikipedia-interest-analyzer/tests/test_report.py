"""Report rendering: HTML content, the strict one-page PDF, and overflow safety.

The one-page constraint is enforced by construction: layout advances an
explicit cursor and every block reserves the space it needs *before* drawing.
If the content cannot fit, ``ReportOverflow`` is raised and no PDF is written
-- a two-page report is never emitted silently.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import i18n
import pytest
import report as report_mod
from helpers import full_translation


def _page_count(path) -> int:
    data = path.read_bytes()
    # reportlab writes plain objects (no object streams), so each page object
    # is visible as /Type /Page -- but /Type /Pages (the tree node) must not
    # be counted, hence the word boundary.
    return len(re.findall(rb"/Type\s*/Page\b", data))


# ------------------------------------------------------------------- HTML -----
def test_html_contains_the_verdict_table_and_provenance(golden_analysis, tmp_path):
    out = tmp_path / "report.html"
    report_mod.render_html(golden_analysis, None, out)

    html = out.read_text(encoding="utf-8")
    assert "intermittent fasting" in html
    assert "Přerušovaný půst" in html, "accented titles must survive to the report"
    assert "38,863" in html
    assert "-49.0%" in html
    assert "ASSUMPTIONS" in html.upper()
    assert "CC0" in html
    assert "Q1666254" in html


def test_html_declares_the_substitution(golden_analysis, tmp_path):
    out = tmp_path / "report.html"
    report_mod.render_html(golden_analysis, None, out)
    html = out.read_text(encoding="utf-8")
    assert "no native article exists" in html
    assert "not like-for-like" in html


def test_html_shows_the_gap_as_a_visible_row(gap_analysis, tmp_path):
    out = tmp_path / "report.html"
    report_mod.render_html(gap_analysis, None, out)
    html = out.read_text(encoding="utf-8")
    assert "no article exists" in html
    assert "not measurable" in html
    assert "never replaced by a substitute" in html


def test_html_escapes_hostile_text(golden_analysis, tmp_path):
    hostile = {**golden_analysis, "topic": "<script>alert(1)</script>"}

    out = tmp_path / "report.html"
    report_mod.render_html(hostile, None, out)

    html = out.read_text(encoding="utf-8")
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


# ------------------------------------------------- editorial HTML components ---
def test_html_kpi_tiles_carry_the_real_numbers(golden_analysis, tmp_path):
    """Each measured language gets a tile with the numbers from analysis.json."""
    out = tmp_path / "report.html"
    report_mod.render_html(golden_analysis, None, out)
    html = out.read_text(encoding="utf-8")

    assert '<section class="kpis">' in html
    # The hero figure is the YoY share the verdict is built from.
    assert report_mod.fmt_pct(
        golden_analysis["metrics"]["cs"]["yoy"]["share_pct"]
    ) in html
    for language, metric in golden_analysis["metrics"].items():
        assert f"<span class=\"tile-lang\">{language}</span>" in html
        assert f"{metric['article_total']:,}" in html, "views must be the real count"
    assert "YoY share" in html
    assert "Total views" in html
    assert "Share per million" in html


def test_html_kpi_trend_arrow_matches_the_sign_of_the_number(
    golden_analysis, tmp_path
):
    """The hero shows arrow + class + number; all three must agree on direction.

    A tile that coloured "+4.1%" red (or drew an up-arrow next to it) would
    invert the verdict the reader takes away, so each tile's markup is checked
    against the sign of its own value.
    """
    out = tmp_path / "report.html"
    report_mod.render_html(golden_analysis, None, out)
    html = out.read_text(encoding="utf-8")

    heroes = re.findall(
        r'<div class="tile-hero (\w+)">'
        r'<span class="arrow" aria-hidden="true">(.*?)</span>([^<]*)</div>',
        html,
    )
    assert len(heroes) == len(golden_analysis["metrics"])
    for kind, arrow, shown in heroes:
        expected_arrow = report_mod.trend_arrow(kind)
        assert arrow == expected_arrow, f"{shown}: arrow disagrees with class {kind}"
        if kind == "up":
            assert shown.startswith("+"), f"{shown} is not a rise"
        elif kind == "down":
            assert shown.startswith("-"), f"{shown} is not a fall"
        else:
            assert shown == report_mod.fmt_pct(None), f"{shown} must read as n/a"

    # Arrow glyphs are only drawn for a direction that exists.
    assert report_mod.trend_arrow("up") == "▲"
    assert report_mod.trend_arrow("down") == "▼"
    assert report_mod.trend_arrow("flat") == "", "no data must not fake a direction"


def test_html_embeds_a_sparkline_for_every_measured_language(
    golden_analysis, tmp_path
):
    out = tmp_path / "report.html"
    report_mod.render_html(golden_analysis, None, out)
    html = out.read_text(encoding="utf-8")

    assert html.count("<svg viewBox=") == len(golden_analysis["metrics"])
    assert 'aria-label="cs share per million, month by month"' in html


def test_a_gap_language_gets_a_tile_that_says_it_is_not_measurable(
    gap_analysis, tmp_path
):
    out = tmp_path / "report.html"
    report_mod.render_html(gap_analysis, None, out)
    html = out.read_text(encoding="utf-8")

    assert '<article class="tile gap-tile">' in html
    assert '<span class="tile-lang">pl</span>' in html
    # The gap tile must not show a fabricated number.
    gap_tile = html.split('<article class="tile gap-tile">')[1].split("</article>")[0]
    assert "no article exists" in gap_tile
    assert report_mod.trend_arrow("flat") == ""
    assert "▲" not in gap_tile and "▼" not in gap_tile


def test_gap_rows_carry_the_row_level_gap_class(gap_analysis, tmp_path):
    """The CSS paints a gap row red/italic only if the row says it is one."""
    out = tmp_path / "report.html"
    report_mod.render_html(gap_analysis, None, out)
    html = out.read_text(encoding="utf-8")

    assert '<tr class="gap-row">' in html
    assert html.count('<tr class="gap-row">') == len(gap_analysis["gaps"])


# ------------------------------------------------------------- table key -----
#: A manifest block that defines every abbreviated column plus the heading --
#: the shape ``study.json`` is expected to carry once the agent has written it.
COMPLETE_TABLE_KEY = {
    "heading": "[xx] Table key",
    "Share/M": "[xx] share per million definition",
    "YoY": "[xx] YoY definition",
    "YoY share": "[xx] YoY share definition",
    "R²": "[xx] R2 definition",
}


def test_the_columns_add_up_to_the_content_width():
    """A grid that misses the page edge would shift every value off its rule."""
    assert sum(width for _, width in report_mod.TABLE_COLUMNS) == (
        report_mod.CONTENT_W
    )


def test_every_header_fits_the_column_it_prints_in():
    """Metric names are fixed codes, so they must fit the column they print in.

    The columns are narrow on purpose -- that is the one-page budget -- and
    nothing can be moved into a translation to shorten them, because metric
    names are never translated. If a header outgrows its column here, it
    outgrows it in every report.
    """
    for name, width in report_mod.TABLE_COLUMNS:
        assert report_mod.fit(name, report_mod.FONT_BOLD, 7.8, width - 8) == name, (
            f"{name!r} does not fit the {width}pt column; shorten the header "
            "(its wording belongs in the table key, not in the column)"
        )


def test_the_key_explains_the_abbreviated_columns_and_only_those():
    """Every code that needs decoding has a definition; plain words do not."""
    items = report_mod.table_key_items()
    codes = [code for code, _ in items]

    assert codes == ["Share/M", "YoY", "YoY share", "R²"]
    for _, definition in items:
        assert len(definition) > 20, "a definition must actually define"
    # These read as their own definition and cost no one-page space.
    for plain in ("Lang", "Article", "Views"):
        assert plain not in codes


def test_the_manifest_wording_is_printed_verbatim(golden_analysis, tmp_path):
    """The key is generated, never translated: what the agent wrote is printed.

    Rewriting it (trimming, re-casing, prefixing) would make study.json an
    unreliable source -- the whole point of moving it out of the catalogue.
    """
    out = tmp_path / "report.html"
    report_mod.render_html(
        golden_analysis, None, out, table_key=COMPLETE_TABLE_KEY
    )
    html = out.read_text(encoding="utf-8")

    assert "<h3>[xx] Table key</h3>" in html
    assert "<dt>Share/M</dt><dd>[xx] share per million definition</dd>" in html
    assert "<dt>YoY</dt><dd>[xx] YoY definition</dd>" in html


def test_metric_headers_never_move_with_the_report_language(
    golden_analysis, tmp_path
):
    """A Ukrainian report still reads Share/M and YoY -- codes, not prose."""
    translator = i18n.Translator("xx", full_translation("xx"))
    out = tmp_path / "report.html"

    report_mod.render_html(
        golden_analysis, None, out, translator, table_key=COMPLETE_TABLE_KEY
    )
    html = out.read_text(encoding="utf-8")

    for name, _ in report_mod.TABLE_COLUMNS:
        assert f"<th>{name}</th>" in html, f"{name} must not be translated"
    assert "<th>[xx]" not in html
    # Nothing was left to fall back to: the translation covers the prose and
    # the manifest covers the key.
    assert translator.untranslated == []
    assert "Untranslated text" not in html


def test_a_localised_report_admits_an_english_key_it_had_to_fall_back_to(
    golden_analysis, tmp_path
):
    """No manifest block: the shipped English wording is used *and* flagged."""
    translator = i18n.Translator("xx", full_translation("xx"))
    out = tmp_path / "report.html"

    report_mod.render_html(golden_analysis, None, out, translator)
    html = out.read_text(encoding="utf-8")

    assert "<dt>YoY</dt><dd>Change in article pageviews" in html
    assert "Untranslated text" in html
    assert "table_key" in html


def test_html_prints_the_key_directly_under_the_table(golden_analysis, tmp_path):
    """The decoding belongs with the table it decodes, not with the caveats."""
    out = tmp_path / "report.html"
    report_mod.render_html(golden_analysis, None, out)
    html = out.read_text(encoding="utf-8")

    assert '<section class="table-key">' in html
    table_end = html.index("</table>")
    key_start = html.index('class="table-key"')
    assumptions = html.index("<h2>")
    assert table_end < key_start < assumptions, "the key must sit under the table"
    assert "<dt>YoY</dt><dd>Change in article pageviews" in html
    assert html.count("<dt>") == len(report_mod.table_key_items())


def test_the_pdf_draws_the_key_under_the_table(golden_analysis, tmp_path, monkeypatch):
    """The PDF is checked through the text it lays out, not its byte stream."""
    drawn: list[str] = []
    original = report_mod._Sheet.text

    def spy(self, value, *args, **kwargs):
        drawn.append(value)
        return original(self, value, *args, **kwargs)

    monkeypatch.setattr(report_mod._Sheet, "text", spy)
    report_mod.render_pdf(
        golden_analysis, None, tmp_path / "report.pdf", table_key=COMPLETE_TABLE_KEY
    )

    key_line = next(i for i, text in enumerate(drawn) if text == "[xx] Table key")
    limitations = next(i for i, text in enumerate(drawn) if "ASSUMPTIONS" in text)
    assert key_line < limitations, "the key must be drawn before the caveats"

    entries = drawn[key_line + 1 : limitations]
    assert any(text.startswith("YoY — ") for text in entries)
    assert any(text.startswith("Share/M — ") for text in entries)


def test_the_key_codes_are_the_headers_the_table_actually_prints():
    """A key that explained a header the table does not print would be worse
    than no key: the reader would hunt for a column that is not there."""
    items = report_mod.table_key_items(COMPLETE_TABLE_KEY)
    printed = {name for name, _ in report_mod.TABLE_COLUMNS}

    assert [code for code, _ in items] == ["Share/M", "YoY", "YoY share", "R²"]
    assert set(code for code, _ in items) <= printed


def test_an_entry_for_a_column_the_table_never_prints_is_reported(capsys):
    """Work the report silently drops must be named, not swallowed."""
    report_mod.warn_unknown_table_key(
        {"heading": "Key", "Share/M": "ok", "Trend": "not a column"}
    )

    err = capsys.readouterr().err
    assert "Trend" in err
    assert "heading" not in err, "the heading is a valid non-column key"


def test_load_table_key_reads_the_block_and_refuses_a_malformed_one(tmp_path):
    """The block is hand-written, so its shape is checked at the boundary."""
    path = tmp_path / "study.json"
    path.write_text(
        json.dumps({"topic": "x", "table_key": {"heading": "Key", "YoY": "why"}}),
        encoding="utf-8",
    )
    assert report_mod.load_table_key(path) == {"heading": "Key", "YoY": "why"}
    # A report stage on its own has no manifest to read -- that is not an error.
    assert report_mod.load_table_key(tmp_path / "absent.json") is None

    path.write_text(
        json.dumps({"table_key": ["not", "an", "object"]}), encoding="utf-8"
    )
    with pytest.raises(SystemExit) as excinfo:
        report_mod.load_table_key(path)
    assert "must be an object" in str(excinfo.value)

    path.write_text(json.dumps({"table_key": {"YoY": 5}}), encoding="utf-8")
    with pytest.raises(SystemExit) as excinfo:
        report_mod.load_table_key(path)
    assert "must be strings" in str(excinfo.value)


def test_confidence_pill_uses_the_grade_for_style_and_the_label_for_words():
    """The grade picks the colour; only the label is translatable text."""
    assert report_mod.confidence_pill_html("high", "HIGH") == (
        '<span class="pill pill-high">HIGH</span>'
    )
    assert report_mod.confidence_pill_html("medium", "MEDIUM") == (
        '<span class="pill pill-medium">MEDIUM</span>'
    )
    assert report_mod.confidence_pill_html("low", "LOW") == (
        '<span class="pill pill-low">LOW</span>'
    )
    assert report_mod.confidence_pill_html("gap", "not measurable") == (
        '<span class="pill pill-gap">not measurable</span>'
    )
    # An unknown grade degrades to a visible badge rather than dropping it.
    assert report_mod.confidence_pill_html("bogus", "X") == (
        '<span class="pill pill-gap">X</span>'
    )


def test_sparkline_refuses_to_draw_a_trend_from_too_few_points():
    """Two points cannot show a trend; one cannot show anything at all."""
    assert report_mod.sparkline_svg([], "#1f77b4", "alt") == ""
    assert report_mod.sparkline_svg([1.0], "#1f77b4", "alt") == ""
    svg = report_mod.sparkline_svg([1.0, 2.0, 3.0], "#1f77b4", "alt")
    assert svg.startswith("<svg")
    assert 'stroke="#1f77b4"' in svg
    assert 'aria-label="alt"' in svg


def test_flat_series_still_draws_a_line():
    """A perfectly flat series has a real span of zero -- it must not divide by it."""
    svg = report_mod.sparkline_svg([5.0, 5.0, 5.0], "#1f77b4", "alt")
    assert svg != ""
    assert "nan" not in svg.lower()


# ------------------------------------------------------------------- PDF ------
def test_pdf_is_exactly_one_letter_page(golden_analysis, tmp_path):
    out = tmp_path / "report.pdf"
    report_mod.render_pdf(golden_analysis, None, out)

    assert out.is_file()
    assert out.stat().st_size > 4_000, "a suspiciously small PDF suggests failure"
    assert _page_count(out) == 1, "reports are strictly one page"


def test_pdf_refuses_to_overflow_and_writes_nothing(golden_analysis, tmp_path):
    """An overstuffed report must fail loudly rather than spill to page 2."""
    bloated = dict(golden_analysis)
    # Distinct items: caveat_items() deliberately drops exact duplicates, so
    # repeating one string would no longer model an over-long report.
    bloated["limitations"] = [
        f"Caveat {index}: this line deliberately repeats itself at great length "
        "in order to exhaust the available page area for the purposes of this "
        "test, padding it well past what any real study would produce. " * 6
        for index in range(15)
    ]

    out = tmp_path / "report.pdf"
    with pytest.raises(report_mod.ReportOverflow) as excinfo:
        report_mod.render_pdf(bloated, None, out)

    assert "does not fit on one page" in str(excinfo.value)
    assert not out.exists(), "no partial PDF may be left behind"


def test_gap_report_also_fits_on_one_page(gap_analysis, tmp_path):
    out = tmp_path / "report.pdf"
    report_mod.render_pdf(gap_analysis, None, out)
    assert _page_count(out) == 1


# --- second cheap-model pass: warnings must reach the shareable artifact ----
def test_caveat_items_put_warnings_first_and_drop_duplicates(golden_analysis):
    # Copy, never mutate: fixture output is shared setup, not scratch space.
    analysis = {
        **golden_analysis,
        "warnings": ["window of 44 months ... capped at medium"],
        "limitations": ["same bullet", "other"],
        "assumptions": ["same bullet", "another"],
    }

    items = report_mod.caveat_items(analysis)

    assert items[0] == "Warning: window of 44 months ... capped at medium"
    assert items.count("same bullet") == 1, "duplicates cost one-page space"
    assert items.index("other") < items.index("another")


def test_html_renders_the_runtime_warnings_json_only_used_to_carry(
    golden_analysis, tmp_path
):
    """A confidence cap a PDF reader cannot see is not a disclosed assumption."""
    analysis = {
        **golden_analysis,
        "warnings": [
            "window of 44 months compares non-identical calendar months, so "
            "confidence is capped at medium; use 23 or 24 months"
        ],
    }

    html_path = tmp_path / "report.html"
    report_mod.render_html(analysis, None, html_path)
    html = html_path.read_text(encoding="utf-8")

    assert "Warning:" in html
    assert "capped at medium" in html


def test_pdf_fits_the_extra_warning_bullet_and_stays_on_one_page(
    golden_analysis, tmp_path
):
    """The PDF consumes the same warning list, and the extra bullet still fits."""
    analysis = {
        **golden_analysis,
        "warnings": [
            "window of 44 months compares non-identical calendar months, so "
            "confidence is capped at medium; use 23 or 24 months"
        ],
    }

    pdf_path = tmp_path / "report.pdf"
    report_mod.render_pdf(analysis, None, pdf_path)

    assert _page_count(pdf_path) == 1


def test_caveat_items_state_the_growth_definition_once(golden_analysis):
    """Both builders used to state the growth definition, wasting page space."""
    growth = [
        item
        for item in report_mod.caveat_items(golden_analysis)
        if "second half" in item
    ]

    assert len(growth) == 1, growth


def test_html_states_the_growth_definition_once(golden_analysis, tmp_path):
    out = tmp_path / "report.html"
    report_mod.render_html(golden_analysis, None, out)

    assert out.read_text(encoding="utf-8").count("Growth compares the second half") == 1


def test_report_and_analysis_share_one_caveat_source():
    """Both renderers must go through caveat_items, not their own copies."""
    source = Path(report_mod.__file__).read_text(encoding="utf-8")
    assert source.count("caveat_items(") >= 3, "definition + HTML + PDF"


# ------------------------------------------------------------- shared model ---
def test_table_rows_describe_both_languages_and_the_gap(gap_analysis):
    rows = report_mod.table_rows_for(gap_analysis)
    assert [row["Lang"] for row in rows] == ["cs", "pl"]
    gap_row = next(row for row in rows if row["Lang"] == "pl")
    assert gap_row["gap"] == "gap"
    assert gap_row["Confidence"] == "not measurable"

    data_row = next(row for row in rows if row["Lang"] == "cs")
    assert data_row["Article"] == "Přerušovaný půst"
    assert data_row["Confidence"] == "MEDIUM"
    assert data_row["grade"] == "medium", "the pill class comes from the grade"


def test_fmt_pct_renders_signed_one_decimal_and_none_for_missing():
    assert report_mod.fmt_pct(-17.27) == "-17.3%"
    assert report_mod.fmt_pct(4.07) == "+4.1%"
    assert report_mod.fmt_pct(None) == "n/a"


def test_wrap_breaks_on_word_boundaries_and_never_exceeds_the_width():
    text = "one two three four five six seven eight nine ten"
    max_width = 60.0
    lines = report_mod.wrap(text, report_mod.FONT, 9.0, max_width)

    assert len(lines) > 1
    assert " ".join(lines) == text, "wrapping must not lose or reorder words"
    for line in lines:
        assert (
            report_mod.pdfmetrics.stringWidth(line, report_mod.FONT, 9.0) <= max_width
        )


def test_accented_title_is_measured_by_the_unicode_font_not_helvetica():
    """Czech and Polish titles must render: Helvetica would tofu these glyphs.

    A different width proves the loaded DejaVu-based font answered instead.
    The check is unconditional, so a font rename fails loudly instead of
    quietly skipping the only assertion with teeth.
    """
    width = report_mod.pdfmetrics.stringWidth("Přerušovaný půst", report_mod.FONT, 9.0)
    helvetica = report_mod.pdfmetrics.stringWidth("Přerušovaný půst", "Helvetica", 9.0)

    assert width != helvetica


def test_project_font_is_registered_before_anything_is_rendered():
    """reportlab falls back silently when the font was never registered."""
    assert report_mod.FONT in report_mod.pdfmetrics.getRegisteredFontNames()


# ------------------------------------------------------- report language -----
def test_english_report_carries_no_untranslated_note(golden_analysis, tmp_path):
    out = tmp_path / "report.html"
    report_mod.render_html(golden_analysis, None, out)

    assert "Untranslated text" not in out.read_text(encoding="utf-8")


def test_report_renders_in_the_requested_language(golden_analysis, tmp_path):
    """Every reader-facing string must come from the translation, not English."""
    translator = i18n.Translator("xx", full_translation("xx"))
    out = tmp_path / "report.html"

    report_mod.render_html(
        golden_analysis, None, out, translator, table_key=COMPLETE_TABLE_KEY
    )
    html = out.read_text(encoding="utf-8")

    assert '<html lang="xx">' in html
    assert "<h1>[xx] Audience interest: intermittent fasting</h1>" in html
    assert "<h2>[xx] Assumptions &amp; limitations</h2>" in html
    # Metric names are codes: they are the one thing in the table that the
    # report language must not touch (the manifest covers their wording).
    assert "<th>Confidence</th>" in html
    # The English wording is gone from the translated parts...
    assert "<h1>Audience interest:" not in html
    # ...but the data itself is untouched: numbers, titles, codes.
    assert "38,863" in html
    assert "Přerušovaný půst" in html
    assert "Q1666254" in html
    # Nothing fell back, so no note is shown.
    assert "Untranslated text" not in html


def test_a_partial_translation_is_flagged_inside_the_report(
    golden_analysis, tmp_path
):
    """A report that is half English must say so where the reader is looking."""
    translator = i18n.Translator(
        "pl",
        {"report.title": "Zainteresowanie odbiorców: {topic}"},
    )
    out = tmp_path / "report.html"

    report_mod.render_html(
        golden_analysis, None, out, translator, table_key=COMPLETE_TABLE_KEY
    )
    html = out.read_text(encoding="utf-8")

    assert "<h1>Zainteresowanie odbiorców: intermittent fasting</h1>" in html
    assert "<th>Lang</th>" in html, "metric names are not part of the catalogue"
    assert '<p class="gap"><strong>Warning</strong> (pl): Untranslated text:' in html
    # The note must name what is missing, not just count it. It lists at most
    # five ids (the reader gets a readable sentence, not a wall), so the
    # assertion checks the shape rather than a fixed membership.
    note = re.search(r'<p class="gap"><strong>Warning.*?</p>', html, re.S).group(0)
    assert re.search(r"\b[a-z]+\.[a-z_]+\b", note), "the note must name missing ids"
    assert re.search(r"\+\d+ more", note), "an over-long list must be summarised"


def test_the_untranslated_note_does_not_break_the_one_page_pdf(
    golden_analysis, tmp_path
):
    translator = i18n.Translator("pl", {"report.title": "Raport: {topic}"})
    out = tmp_path / "report.pdf"

    report_mod.render_pdf(golden_analysis, None, out, translator)

    assert out.is_file()
    assert _page_count(out) == 1


def test_a_localised_report_still_fits_one_page(golden_analysis, tmp_path):
    translator = i18n.Translator("xx", full_translation("xx"))
    out = tmp_path / "report.pdf"

    report_mod.render_pdf(golden_analysis, None, out, translator)

    assert out.is_file()
    assert _page_count(out) == 1


def test_the_localised_headline_comes_from_the_refs_not_the_english_string(
    golden_analysis, tmp_path
):
    translator = i18n.Translator(
        "xx",
        full_translation(
            "xx",
            **{
                "headline.main": "VERDICT {topic} {span}: {parts}. "
                "GRADE {grades}."
            },
        ),
    )
    out = tmp_path / "report.html"

    report_mod.render_html(golden_analysis, None, out, translator)
    html = out.read_text(encoding="utf-8")

    assert "VERDICT &#x27;intermittent fasting&#x27;" in html
    assert golden_analysis["headline"] not in html


def test_fit_shortens_a_header_that_would_collide_with_its_neighbour():
    long = "A translated header that is far too wide for this narrow column"
    fitted = report_mod.fit(long, report_mod.FONT_BOLD, 7.8, 30.0)

    assert len(fitted) < len(long)
    assert fitted.endswith("\u2026")
    assert (
        report_mod.pdfmetrics.stringWidth(fitted, report_mod.FONT_BOLD, 7.8) <= 30.0
    ), "a header must never spill into the next column"


def test_fit_leaves_a_header_that_already_fits_untouched():
    assert report_mod.fit("Confidence", report_mod.FONT_BOLD, 7.8, 100.0) == (
        "Confidence"
    )


# ------------------------------------------------------- optional columns ---
def _with_layers(analysis, layers=("access", "bot", "top")):
    """A copy of an analysis carrying the metrics those layers would produce."""
    for metric in analysis["metrics"].values():
        if "access" in layers:
            metric["access_split"] = {
                "total_views": metric["article_total"],
                "desktop_pct": 40.0,
                "mobile_web_pct": 30.0,
                "mobile_app_pct": 30.0,
                "mobile_pct": 60.0,
            }
        if "bot" in layers:
            metric["bot_share_pct"] = 12.5
        if "top" in layers:
            metric["top_rank"] = {
                "month": "2026-08",
                "rank": 7,
                "list_size": 1000,
            }
    return analysis


def test_the_base_grid_is_unchanged_when_no_layer_was_measured(golden_analysis):
    """A study without layers must render the catalogue's grid, byte for byte."""
    assert report_mod.table_columns_for(golden_analysis) == report_mod.TABLE_COLUMNS


@pytest.mark.parametrize(
    "layers",
    [
        ("access",),
        ("bot",),
        ("top",),
        ("access", "bot"),
        ("access", "top"),
        ("bot", "top"),
        ("access", "bot", "top"),
    ],
)
def test_every_layer_combination_fills_the_page_and_fits_its_headers(
    golden_analysis, layers
):
    """Extra columns may never widen the grid or truncate a header.

    The one-page budget is the whole design: the columns re-fit to the page
    width by giving up room in proportion to their slack, and no header is
    allowed to be shortened away from its own name.
    """
    columns = report_mod.table_columns_for(_with_layers(golden_analysis, layers))

    assert sum(width for _, width in columns) == report_mod.CONTENT_W
    assert [name for name, _ in columns] == [
        name for name, _ in report_mod.TABLE_COLUMNS
    ] + _with_layers_headers(layers)
    for name, width in columns:
        assert report_mod.fit(name, report_mod.FONT_BOLD, 7.8, width - 8) == name, (
            f"{name!r} does not fit the {width}pt column"
        )


def _with_layers_headers(layers):
    names = []
    if "access" in layers:
        names.append("Mobile %")
    if "bot" in layers:
        names.append("Bot %")
    if "top" in layers:
        names.append("Rank")
    return names


def test_layer_columns_carry_the_layer_values(golden_analysis, tmp_path):
    """The numbers the layers measured reach the reader, correctly formatted."""
    out = tmp_path / "report.html"
    report_mod.render_html(_with_layers(golden_analysis), None, out)
    html = out.read_text(encoding="utf-8")

    assert "<th>Mobile %</th>" in html
    assert "<th>Bot %</th>" in html
    assert "<th>Rank</th>" in html
    assert "<td>60.0%</td>" in html
    assert "<td>12.5%</td>" in html
    assert "<td>7</td>" in html


def test_a_language_ranked_outside_the_list_reads_as_outside_it(
    golden_analysis, tmp_path
):
    """``rank: None`` means "not in the top N" -- a result, never a blank."""
    _with_layers(golden_analysis)
    golden_analysis["metrics"]["cs"]["top_rank"] = {
        "month": "2026-08",
        "rank": None,
        "list_size": 1000,
    }
    rows = report_mod.table_rows_for(golden_analysis)

    ranked = next(row for row in rows if row["Lang"] == "cs")
    unranked = next(row for row in rows if row["Lang"] == "pl")
    assert ranked["Rank"] == ">1000"
    assert unranked["Rank"] == "7"


def test_a_gap_row_stays_a_dash_in_every_layer_column(gap_analysis):
    """A language with no article must not appear to have a layer value."""
    _with_layers(gap_analysis)
    rows = report_mod.table_rows_for(gap_analysis, columns=None)

    gap_row = next(row for row in rows if row["gap"])
    for name in ("Mobile %", "Bot %", "Rank"):
        assert gap_row[name] == "\u2014"


def test_the_key_explains_only_the_columns_that_are_printed(golden_analysis):
    """An unprinted layer column costs no one-page space and says nothing."""
    base = report_mod.table_key_items()
    assert [code for code, _ in base] == ["Share/M", "YoY", "YoY share", "R\u00b2"]

    layered = report_mod.table_key_items(
        columns=report_mod.table_columns_for(_with_layers(golden_analysis))
    )
    codes = [code for code, _ in layered]
    assert "Mobile %" in codes
    assert "Bot %" in codes
    assert "Rank" in codes
    # The definitions come from the defaults until the manifest writes its own.
    assert all(len(definition) > 20 for _, definition in layered)


def test_warn_unknown_table_key_understands_optional_columns(capsys):
    """A layer definition is honoured only when its column is actually shown."""
    report_mod.warn_unknown_table_key({"Mobile %": "[xx] wording"})
    assert "Mobile %" in capsys.readouterr().err

    columns = report_mod.OPTIONAL_TABLE_COLUMNS
    report_mod.warn_unknown_table_key(
        {"Mobile %": "[xx] wording"},
        columns=report_mod.TABLE_COLUMNS
        + [("Mobile %", columns["Mobile %"])],
    )
    assert capsys.readouterr().err == ""


def test_the_ranking_sentence_names_the_criterion(golden_analysis, tmp_path):
    """A reordered table must say who reordered it, in the report's language."""
    golden_analysis["comparison"]["ranked_by"] = {
        "criterion": "bot_share",
        "order": ["cs", "pl"],
    }
    out = tmp_path / "report.html"
    report_mod.render_html(golden_analysis, None, out)
    html = out.read_text(encoding="utf-8")

    assert 'class="ranked"' in html
    assert "Ranked by human traffic (lowest bot share), best first." in html


def test_without_a_criterion_no_ranking_sentence_is_printed(
    golden_analysis, tmp_path
):
    out = tmp_path / "report.html"
    report_mod.render_html(golden_analysis, None, out)
    assert 'class="ranked"' not in out.read_text(encoding="utf-8")


def test_the_table_rows_follow_the_stated_ranking(golden_analysis):
    """The sentence and the order must agree: the table shows ``order``."""
    golden_analysis["comparison"]["ranked_by"] = {
        "criterion": "bot_share",
        "order": ["cs", "pl"],
    }
    rows = report_mod.table_rows_for(golden_analysis)

    measured = [row["Lang"] for row in rows if not row["gap"]]
    assert measured == ["cs", "pl"]


def test_the_layered_one_page_pdf_still_fits(golden_analysis, tmp_path):
    """Three extra columns and a ranking sentence cost no extra page."""
    _with_layers(golden_analysis)
    golden_analysis["comparison"]["ranked_by"] = {
        "criterion": "bot_share",
        "order": ["cs", "pl"],
    }
    out = tmp_path / "report.pdf"

    report_mod.render_pdf(golden_analysis, None, out)

    assert out.exists()
    assert _page_count(out) == 1


def test_the_layered_html_carries_the_key_and_the_ranking(
    golden_analysis, tmp_path
):
    """Both edits that sit *under* the table travel together in one render."""
    _with_layers(golden_analysis)
    golden_analysis["comparison"]["ranked_by"] = {
        "criterion": "mobile_share",
        "order": ["pl", "cs"],
    }
    out = tmp_path / "report.html"
    report_mod.render_html(golden_analysis, None, out, table_key=COMPLETE_TABLE_KEY)
    html = out.read_text(encoding="utf-8")

    assert "Ranked by mobile share of views, best first." in html
    assert "<dt>Mobile %</dt>" in html


def test_gap_rows_trail_the_ranked_languages(gap_analysis):
    """Reordering the measured languages must never promote a gap row."""
    gap_analysis["comparison"]["ranked_by"] = {
        "criterion": "share_ppm",
        "order": ["cs"],
    }
    rows = report_mod.table_rows_for(gap_analysis)

    assert [(row["Lang"], row["gap"]) for row in rows] == [("cs", ""), ("pl", "gap")]


def test_the_full_layered_payload_still_fits_one_page(golden_analysis, tmp_path):
    """Metrics are not the whole cost: the layer *wording* must fit too.

    `analyze.py` appends one limitation per measured layer and the report
    prints its ranking sentence, and a real run also embeds the chart -- all
    three eat the one-page budget together. Rendering the metrics alone with
    no chart (as an earlier version of this test did) hid a PDF that
    overflowed by a point in actual use.
    """
    import chart as chart_mod

    _with_layers(golden_analysis)
    # Exactly what analyze.py appends when all three layers were measured.
    ref = {"id": "limitation.layers_all"}
    golden_analysis["limitations"] += [i18n.english().render(ref)]
    golden_analysis["limitations_i18n"] += [ref]
    golden_analysis["comparison"]["ranked_by"] = {
        "criterion": "bot_share",
        "order": ["cs", "pl"],
    }
    chart_png = Path(chart_mod.render(golden_analysis, tmp_path / "chart")["png"])
    out = tmp_path / "report.pdf"

    report_mod.render_pdf(golden_analysis, chart_png, out)

    assert _page_count(out) == 1
