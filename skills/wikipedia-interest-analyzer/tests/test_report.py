"""Report rendering: HTML content, the strict one-page PDF, and overflow safety.

The one-page constraint is enforced by construction: layout advances an
explicit cursor and every block reserves the space it needs *before* drawing.
If the content cannot fit, ``ReportOverflow`` is raised and no PDF is written
-- a two-page report is never emitted silently.
"""

from __future__ import annotations

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

    report_mod.render_html(golden_analysis, None, out, translator)
    html = out.read_text(encoding="utf-8")

    assert '<html lang="xx">' in html
    assert "<h1>[xx] Audience interest: intermittent fasting</h1>" in html
    assert "<h2>[xx] Assumptions &amp; limitations</h2>" in html
    assert "<th>[xx] Confidence</th>" in html
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
        {
            "report.title": "Zainteresowanie odbiorców: {topic}",
            "report.col_lang": "Język",
        },
    )
    out = tmp_path / "report.html"

    report_mod.render_html(golden_analysis, None, out, translator)
    html = out.read_text(encoding="utf-8")

    assert "<h1>Zainteresowanie odbiorców: intermittent fasting</h1>" in html
    assert "<th>Język</th>" in html
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
