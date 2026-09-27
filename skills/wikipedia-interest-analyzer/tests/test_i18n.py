"""Report language plumbing: catalog integrity, fallbacks, and the visible note.

The contract the skill advertises is narrow and checkable: an agent supplies
translations for every message id, anything missing renders in English *and*
says so, a translation that would swallow a ``{placeholder}`` is rejected
rather than printed, and the English strings in ``analysis.json`` really are
the rendering of the refs stored beside them.
"""

from __future__ import annotations

import json
import re

import i18n
import pytest
from helpers import full_translation


# --------------------------------------------------------------- catalog -----
def test_every_message_template_has_balanced_braces():
    for key, template in i18n.MESSAGES.items():
        try:
            i18n.placeholders(template)
        except ValueError as exc:  # pragma: no cover - fails the assertion
            pytest.fail(f"{key}: malformed template: {exc}")


def test_join_separators_carry_no_placeholders():
    """A separator is inserted between items; a ``{}`` in one would crash."""
    for key in ("join.comma", "join.semicolon"):
        assert not i18n.placeholders(i18n.MESSAGES[key])


def test_unknown_message_id_is_a_programming_error():
    translator = i18n.Translator("pl", {"report.title": "x"})
    with pytest.raises(KeyError) as excinfo:
        translator.t("report.does_not_exist")
    assert "i18n.MESSAGES" in str(excinfo.value)


# ------------------------------------------------------------ fallbacks ------
def test_missing_translation_falls_back_to_english_and_is_recorded():
    translator = i18n.Translator("pl", {})

    assert translator.t("report.no_data") == "no data"
    assert translator.untranslated == ["report.no_data"]
    # The note names the *id*, so the agent knows what to add to the file.
    assert "report.no_data" in (translator.note() or "")


def test_english_translator_records_nothing():
    translator = i18n.english()
    translator.t("report.no_data")

    assert translator.untranslated == []
    assert translator.note() is None


def test_placeholder_mismatch_is_rejected_instead_of_printed():
    """A dropped ``{span}`` must not become a sentence with a hole in it."""
    translator = i18n.Translator(
        "pl", {"headline.none": "Brak danych o {topic}."}  # {span} missing
    )

    rendered = translator.t("headline.none", topic="'x'", span="2024-10..2026-08")

    assert rendered.startswith("No language edition"), "English fallback expected"
    assert "headline.none" in translator.untranslated


def test_a_sound_translation_is_used_verbatim():
    translator = i18n.Translator(
        "pl", {"headline.none": "Brak danych o {topic} w {span}."}
    )

    rendered = translator.t("headline.none", topic="'x'", span="2024-10..2026-08")

    assert rendered == "Brak danych o 'x' w 2024-10..2026-08."
    assert translator.untranslated == []


def test_a_translation_with_an_impossible_format_spec_falls_back():
    """Right placeholders, wrong type: reject rather than crash the report."""
    translator = i18n.Translator("xx", {"report.footer": "Dane {qid:d}."})

    rendered = translator.t("report.footer", qid="Q1")

    assert rendered.startswith("Data: Wikimedia")
    assert translator.untranslated == ["report.footer"]


def test_non_string_values_count_as_missing(tmp_path):
    path = tmp_path / "translations.pl.json"
    path.write_text(
        json.dumps({"messages": {"report.no_data": 5}}), encoding="utf-8"
    )

    messages, malformed = i18n.load_messages(path)
    translator = i18n.Translator("pl", messages, malformed=malformed)

    assert translator.malformed == ["report.no_data"]
    assert translator.t("report.no_data") == "no data"
    assert translator.untranslated == ["report.no_data"]


def test_unknown_ids_in_a_file_are_ignored_not_treated_as_missing():
    translator = i18n.Translator(
        "pl", {"report.no_data": "brak danych", "my.own.note": "hi"}
    )

    translator.t("report.no_data")

    assert translator.untranslated == [], "only ids the report asks for count"
    assert translator.ignored == ["my.own.note"]


def test_the_note_names_at_most_five_missing_ids():
    translator = i18n.Translator("xx", {})
    for key in list(i18n.MESSAGES)[:8]:
        params = {name: "x" for name in i18n.placeholders(i18n.MESSAGES[key])}
        translator.t(key, **params)

    note = translator.note()

    assert note is not None
    assert "+3 more" in note, "the note must stay short enough to read"
    assert note.count(",") <= 6


def test_a_complete_translation_has_no_note():
    translator = i18n.Translator("xx", full_translation())

    assert translator.untranslated == []
    assert translator.note() is None


# ------------------------------------------------------- translations file ----
def test_reference_file_round_trips(tmp_path):
    path = tmp_path / "translations.pl.json"

    assert i18n.write_reference(path, "pl") is True

    messages, malformed = i18n.load_messages(path)
    assert set(messages) == set(i18n.MESSAGES)
    assert malformed == []
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["lang"] == "pl"
    assert "{placeholder}" in payload["_instructions"]


def test_reference_file_refuses_to_clobber_a_finished_translation(tmp_path):
    path = tmp_path / "translations.pl.json"
    i18n.write_reference(path, "pl")

    with pytest.raises(SystemExit) as excinfo:
        i18n.write_reference(path, "pl")
    assert "--force" in str(excinfo.value)

    assert i18n.write_reference(path, "pl", force=True) is True


def test_corrupt_translations_stop_the_run_instead_of_going_english(tmp_path):
    path = tmp_path / "translations.pl.json"
    path.write_text('{"messages": {"report.title": ', encoding="utf-8")

    with pytest.raises(SystemExit) as excinfo:
        i18n.load_messages(path)
    assert "cannot read translations" in str(excinfo.value)


def test_a_missing_file_makes_translator_for_write_the_reference(tmp_path):
    path = tmp_path / "translations.pl.json"

    translator = i18n.translator_for("pl", path)

    assert path.is_file(), "the agent must be handed something to translate"
    assert translator.t("report.no_data") == "no data"
    assert "report.no_data" in translator.untranslated


def test_english_never_touches_the_translations_file(tmp_path):
    path = tmp_path / "translations.en.json"

    translator = i18n.translator_for("en", path)

    assert translator.is_english
    assert not path.exists()


# -------------------------------------------------------------- rendering ----
def test_english_strings_are_the_rendering_of_their_refs(golden_analysis):
    """analysis.json and the report must never disagree about what was said."""
    english = i18n.english()

    assert (
        english.render(golden_analysis["headline_i18n"]) == golden_analysis["headline"]
    )
    assert [english.render(ref) for ref in golden_analysis["limitations_i18n"]] == (
        golden_analysis["limitations"]
    )
    assert [english.render(ref) for ref in golden_analysis["assumptions_i18n"]] == (
        golden_analysis["assumptions"]
    )


def test_a_translated_rendering_keeps_every_number(golden_analysis):
    translator = i18n.Translator("xx", full_translation())

    rendered = translator.render(golden_analysis["headline_i18n"])

    assert rendered.startswith("[xx] Normalised interest")
    for token in re.findall(r"\d{4}-\d{2}", golden_analysis["headline"]):
        assert token in rendered, "the window must survive translation"
    assert translator.untranslated == []


def test_word_order_is_the_translations_not_the_english_ones():
    """The reference is a template: reordering the words must be allowed."""
    translator = i18n.Translator(
        "pl",
        {
            **full_translation("pl"),
            "headline.main": "W temacie {topic} w oknie {span}: {parts}. "
            "Pewność: {grades}.",
        },
    )
    ref = {
        "id": "headline.main",
        "params": {
            "topic": "'fasting'",
            "span": "2024-10..2026-08",
            "parts": {"items": ["pl +1.0% share (+2.0% absolute)"]},
            "grades": {"items": ["pl medium"]},
        },
    }

    rendered = translator.render(ref)

    assert rendered == (
        "W temacie 'fasting' w oknie 2024-10..2026-08: "
        "pl +1.0% share (+2.0% absolute). Pewność: pl medium."
    )


def test_a_group_uses_its_own_separator():
    translator = i18n.Translator("pl", {"join.comma": ", ", "join.semicolon": "; "})
    ref = {
        "id": "headline.gaps",
        "params": {"langs": {"items": ["pl", "cs"], "sep": "join.semicolon"}},
    }

    assert translator.render(ref) == "No article exists in: pl; cs -- not measurable."


def test_a_ref_without_an_id_is_rejected():
    with pytest.raises(TypeError):
        i18n.Translator("pl", {}).render({"params": {}})


def test_a_parameter_dict_of_an_unrecognised_shape_is_rejected():
    with pytest.raises(TypeError):
        i18n.Translator("pl", {}).render(
            {"id": "headline.none", "params": {"topic": {"nonsense": 1}}}
        )


def test_mark_untranslated_reports_payloads_that_predate_the_refs():
    translator = i18n.Translator("pl", dict(full_translation("pl")))

    translator.mark_untranslated("headline_i18n")

    assert translator.untranslated == ["headline_i18n"]
    assert "headline_i18n" in (translator.note() or "")
