"""End-to-end CLI tests: the whole workflow, offline, through ``run.py``.

These are the tests that make the skill safe to hand to a cheap model: they
prove a bare `run.py all` produces every artifact, that a gap is reported
rather than filled, and that a report which cannot fit one page exits non-zero
instead of emitting a two-page PDF.
"""

from __future__ import annotations

import re
from pathlib import Path

import common
import i18n
import pytest
import run as run_mod
from helpers import full_translation

ARTIFACTS = (
    "series.json",
    "analysis.json",
    "chart.png",
    "chart.svg",
    "report.html",
    "report.pdf",
)


GOLDEN_INIT_ARGS = [
    "init",
    "--topic",
    "intermittent fasting",
    "--langs",
    "pl,cs",
    "--since",
    "2024-10",
    "--until",
    "2026-08",
]


def _init(tmp_path) -> str:
    """Arrange: write the golden study manifest.

    No assertion lives here -- the test whose behavior *is* init checks the
    exit code itself, so failure output points at the test, not this helper.
    """
    path = str(tmp_path / "study.json")
    run_mod.main([*GOLDEN_INIT_ARGS, "--study", path])
    return path


def _set_table_key(path: str, marker: str = "") -> None:
    """Arrange: give the manifest its generated (never translated) table key."""
    study = common.read_json(path)
    study["table_key"] = {
        "heading": f"{marker} Table key",
        "Share/M": f"{marker} share definition",
        "YoY": f"{marker} YoY definition",
        "YoY share": f"{marker} YoY share definition",
        "R²": f"{marker} R2 definition",
    }
    common.write_json(path, study)


def test_init_writes_a_manifest(tmp_path):
    path = str(tmp_path / "study.json")
    assert run_mod.main([*GOLDEN_INIT_ARGS, "--study", path]) == 0

    study = common.read_json(path)
    assert study["topic"] == "intermittent fasting"
    assert study["languages"] == ["pl", "cs"]
    assert study["window"] == {"since": "2024-10", "until": "2026-08"}
    assert study["overrides"] == {}
    assert study["granularity"] == "monthly"


def test_init_refuses_to_clobber_an_existing_study(tmp_path):
    path = _init(tmp_path)
    assert run_mod.main(["init", "--topic", "x", "--langs", "pl", "--study", path]) == 1
    assert common.read_json(path)["topic"] == "intermittent fasting"


def test_status_reports_the_resolved_article_and_the_gap(tmp_path, capsys):
    path = _init(tmp_path)
    assert run_mod.main(["all", "--stage", "resolve", "--study", path]) == 0
    capsys.readouterr()  # drop init/resolve chatter, keep only the status output

    assert run_mod.main(["status", "--study", path]) == 0
    status = capsys.readouterr().out

    assert "cs: Přerušovaný půst" in status
    assert "pl: GAP" in status
    assert "candidate:" in status, "a gap must expose candidates to choose from"
    assert (
        "next: ask the user" in status
    ), "a gap must tell the agent to ask, not to decide alone"


def test_a_gap_tells_the_agent_to_ask_in_resolve_output_too(tmp_path, capsys):
    """The gap decision happens right after `resolve`, before `status`."""
    path = _init(tmp_path)
    assert run_mod.main(["all", "--stage", "resolve", "--study", path]) == 0

    out = capsys.readouterr().out
    assert "pl: GAP -- no article for this topic" in out
    assert "next: ask the user" in out
    assert "to analyse one: run.py override" in out


def test_override_is_recorded_in_the_study(tmp_path):
    path = _init(tmp_path)
    assert run_mod.main(["all", "--stage", "resolve", "--study", path]) == 0

    assert (
        run_mod.main(["override", "--study", path, "--lang", "pl", "--title", "Post"])
        == 0
    )

    assert common.read_json(path)["overrides"] == {"pl": "Post"}


def test_status_before_resolution_says_so(tmp_path, capsys):
    path = _init(tmp_path)
    assert run_mod.main(["status", "--study", path]) == 0
    assert "not resolved yet" in capsys.readouterr().out


def test_override_rejects_a_language_outside_the_study(tmp_path):
    path = _init(tmp_path)
    assert (
        run_mod.main(["override", "--study", path, "--lang", "uk", "--title", "x"]) == 1
    )


def test_full_pipeline_produces_every_artifact(tmp_path):
    path = _init(tmp_path)
    assert (
        run_mod.main(["override", "--study", path, "--lang", "pl", "--title", "Post"])
        == 0
    )

    out = tmp_path / "out"
    assert run_mod.main(["all", "--study", path, "--out", str(out)]) == 0

    # The manifest lives beside the invocation, not in the output directory.
    assert Path(path).is_file()
    for name in ARTIFACTS:
        assert (out / name).is_file(), f"missing {name}"

    analysis = common.read_json(out / "analysis.json")
    assert set(analysis["metrics"]) == {"pl", "cs"}
    assert sum(analysis["metrics"]["pl"]["series"]["article_views"]) == 38_863


def test_pipeline_reports_a_gap_end_to_end(tmp_path):
    path = _init(tmp_path)
    out = tmp_path / "out"
    assert run_mod.main(["all", "--study", path, "--out", str(out)]) == 0

    analysis = common.read_json(out / "analysis.json")
    assert analysis["gaps"] == ["pl"]
    assert set(analysis["metrics"]) == {"cs"}
    assert any("never filled with a substitute" in x for x in analysis["limitations"])

    # The gap is visible in the report too, not just in the JSON.
    html = (out / "report.html").read_text(encoding="utf-8")
    assert "no article exists" in html


def test_stage_can_be_run_independently(tmp_path, monkeypatch):
    path = _init(tmp_path)
    monkeypatch.chdir(tmp_path)  # --out defaults to the cwd, so watch it

    assert run_mod.main(["all", "--stage", "resolve", "--study", path]) == 0

    assert common.read_json(path)["resolution"]["qid"] == "Q1666254"
    # resolve must stop before fetch: no artifacts in the study dir or the cwd.
    assert not (tmp_path / "series.json").exists()
    assert not (tmp_path / "analysis.json").exists()


def test_unresolvable_stage_fails_with_a_clear_message(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    study = tmp_path / "study.json"
    common.write_json(study, {"topic": "x", "languages": ["pl"]})
    # No resolution yet: fetch must refuse rather than guess.
    assert (
        run_mod.main(
            ["all", "--stage", "fetch", "--study", str(study), "--out", str(out)]
        )
        == 1
    )


def test_report_stage_exits_non_zero_when_the_pdf_cannot_fit(tmp_path, golden_analysis):
    out = tmp_path / "out"
    out.mkdir()
    study = tmp_path / "study.json"
    common.write_json(
        study, {"topic": "intermittent fasting", "languages": ["pl", "cs"]}
    )

    bloated = dict(golden_analysis)
    bloated["limitations"] = [
        f"Reason {index}: far too much text " + "detail " * 200 for index in range(12)
    ]
    common.write_json(out / "analysis.json", bloated)

    rc = run_mod.main(
        ["all", "--stage", "report", "--study", str(study), "--out", str(out)]
    )

    assert rc == 1, "an over-long report must fail, not silently use two pages"
    assert (out / "report.html").is_file(), "HTML has no page limit"
    assert not (out / "report.pdf").exists()


def test_clear_cache_is_available(tmp_path, monkeypatch, capsys):
    # Redirect the cache so the developer's real cache survives the test run.
    monkeypatch.setattr(common, "CACHE_DIR", tmp_path / "cache")
    assert run_mod.main(["clear-cache"]) == 0
    assert "cached response" in capsys.readouterr().out


def test_bare_invocation_defaults_to_all(tmp_path):
    """`run.py --out dir` must behave like `run.py all --out dir`."""
    path = _init(tmp_path)
    out = tmp_path / "out"
    assert run_mod.main(["--study", path, "--out", str(out)]) == 0
    assert (out / "report.pdf").is_file()


# --- regressions from the cheap-model end-to-end test ------------------------
def test_resolve_subcommand_exists_and_stops_before_fetching(tmp_path):
    """`status` says "run 'run.py resolve'", so `resolve` must be a real command.

    Found by an agent following SKILL.md literally: it was told to run a command
    that did not exist and had to guess `all --stage resolve` from --help.
    """
    path = _init(tmp_path)
    out = tmp_path / "out"
    out.mkdir()

    # `resolve` writes nothing outside the manifest, so it must not advertise --out.
    assert run_mod.main(["resolve", "--study", path]) == 0

    assert common.read_json(path)["resolution"]["qid"] == "Q1666254"
    assert not (out / "series.json").exists(), "resolve must stop before fetching"
    assert not (out / "analysis.json").exists()


def test_messages_never_reference_a_subcommand_that_does_not_exist():
    """Meta-test: no message may name a command the parser does not offer.

    This is the class of bug that made the agent guess: a string like
    "run 'run.py fetch' first" pointing at a subcommand that was never defined.
    """
    source = Path(run_mod.__file__).read_text(encoding="utf-8")
    # Digits are part of a subcommand name too (`i18n-template`), so the
    # pattern must accept them -- otherwise it would flag the very command
    # that writes the translations file as "non-existent".
    referenced = set(re.findall(r"run\.py ([a-z][a-z0-9-]*)", source))
    missing = referenced - set(run_mod.SUBCOMMANDS)
    assert not missing, (
        f"messages reference non-existent subcommands: {sorted(missing)}"
    )


@pytest.mark.parametrize("command", sorted(run_mod.SUBCOMMANDS))
def test_subcommand_offers_a_help_screen(command):
    with pytest.raises(SystemExit) as excinfo:
        run_mod.main([command, "--help"])

    assert excinfo.value.code == 0


def test_status_lists_runner_up_concepts_for_re_picking(tmp_path, capsys):
    """Step 2 exists to catch the wrong Q-item, so the alternatives must show."""
    path = _init(tmp_path)
    assert run_mod.main(["resolve", "--study", path]) == 0
    capsys.readouterr()

    assert run_mod.main(["status", "--study", path]) == 0
    out = capsys.readouterr().out

    assert "hits:" in out
    assert "-> Q1666254" in out, "the chosen concept must be marked"
    assert re.search(r"(?m)^ {2}\s+Q\d+", out), "runner-up concepts must be listed"
    assert "--qid" in out, "the user must be told how to re-pick"


def test_status_shows_the_effective_window_not_default(tmp_path, capsys):
    """`(default) .. (default)` is unreadable; show the dates the run will use."""
    path = str(tmp_path / "study.json")
    assert run_mod.main(["init", "--topic", "x", "--langs", "pl", "--study", path]) == 0

    assert run_mod.main(["status", "--study", path]) == 0
    line = next(
        item
        for item in capsys.readouterr().out.splitlines()
        if item.startswith("window:")
    )

    assert "(default)" not in line, "an unset window must still show real dates"
    assert re.search(r"window:\s+\d{4}-\d{2} \.\. \d{4}-\d{2}", line), line


def test_init_hints_point_at_real_commands(tmp_path, capsys):
    _init(tmp_path)
    out = capsys.readouterr().out
    assert "run.py resolve --study" in out
    assert "run.py all --study" in out and "--out" in out


def test_init_points_at_the_report_language_when_it_was_not_set(tmp_path, capsys):
    """The default is English; the console must say so before resolve runs."""
    path = str(tmp_path / "study.json")
    assert run_mod.main([*GOLDEN_INIT_ARGS, "--study", path]) == 0
    out = capsys.readouterr().out

    assert "report:    (not set)" in out
    assert "--report-lang <the language the user prompted in>" in out


def test_init_prints_the_report_language_it_stored(tmp_path, capsys):
    path = str(tmp_path / "study.json")
    assert (
        run_mod.main([*GOLDEN_INIT_ARGS, "--study", path, "--report-lang", "uk"])
        == 0
    )
    out = capsys.readouterr().out

    assert "report:    uk" in out
    assert "(not set)" not in out


def test_status_repeats_the_missing_report_language_hint(tmp_path, capsys):
    """`status` is the pre-flight check; it must not go quiet on the default."""
    path = _init(tmp_path)
    capsys.readouterr()

    assert run_mod.main(["status", "--study", path]) == 0
    assert "report:    (not set)" in capsys.readouterr().out


def test_init_warns_when_the_window_cannot_be_split_comparably(tmp_path, capsys):
    """The warning must arrive at `init`, not three stages later."""
    path = str(tmp_path / "study.json")
    assert (
        run_mod.main(
            [
                "init",
                "--topic",
                "x",
                "--langs",
                "pl",
                "--study",
                path,
                "--since",
                "2023-01",
                "--until",
                "2026-08",
            ]
        )
        == 0
    )
    captured = capsys.readouterr()
    assert "capped at medium" in captured.err
    assert "2023-01 .. 2026-08" in captured.out


def test_resolve_has_no_out_flag_because_it_writes_no_results():
    """A flag that does nothing is misleading."""
    parser = run_mod.build_parser()
    resolve_parser = next(
        action
        for action in parser._subparsers._group_actions
        if action.dest == "command"
    ).choices["resolve"]
    assert "--out" not in resolve_parser.format_help()


def test_clip_cuts_on_a_word_boundary():
    """A raw [:80] sliced descriptions mid-word (… "Diese Pro)")."""
    text = (
        "Wurde. Es ist einzeln und mit Learning English Lesson Two "
        "erhältlich. Diese Produktion erschien im Jahr 1996 auf CD."
    )
    clipped = common.clip(text, 60)

    assert len(clipped) <= 63
    assert not clipped.endswith("Pro)"), "must not stop mid-word"
    assert clipped.endswith("...")
    assert common.clip("short", 60) == "short"
    # No space to break on -> fall back to a hard cut rather than returning all.
    assert common.clip("x" * 200, 60).startswith("x" * 60)


def test_status_also_surfaces_the_window_warning(tmp_path, capsys):
    path = str(tmp_path / "study.json")
    assert (
        run_mod.main(
            [
                "init",
                "--topic",
                "x",
                "--langs",
                "pl",
                "--study",
                path,
                "--since",
                "2023-01",
                "--until",
                "2026-08",
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert run_mod.main(["status", "--study", path]) == 0
    assert "capped at medium" in capsys.readouterr().err


# ------------------------------------------------- errors must stay visible --
def test_run_main_prints_an_error_instead_of_exiting_silently(tmp_path, capsys):
    """A failure through run.py used to exit 1 with no message at all.

    ``SystemExit("error: ...")`` carries its message as the exit code, and
    run.main() returned 1 without printing it -- so every CLI error routed
    through the documented entry point disappeared.
    """
    missing = str(tmp_path / "no-such-study.json")

    assert run_mod.main(["status", "--study", missing]) == 1

    err = capsys.readouterr().err
    assert "error: study manifest not found" in err
    assert missing in err


def test_status_surfaces_an_invalid_window_instead_of_a_plausible_default(
    tmp_path, capsys
):
    """An unresolvable window must not print `(default) .. (default)`."""
    path = str(tmp_path / "study.json")
    common.write_json(
        path,
        {
            "topic": "x",
            "languages": ["pl"],
            "window": {"since": "2009-01", "until": "2010-01"},
        },  # pre-2015 era
    )

    assert run_mod.main(["status", "--study", path]) == 1

    out = capsys.readouterr()
    assert "no complete months" in out.err
    assert "(default)" not in out.out


def test_init_rejects_an_invalid_window_without_writing_a_manifest(tmp_path, capsys):
    """Validation happens before the write: a rejected window leaves nothing."""
    path = str(tmp_path / "study.json")

    assert (
        run_mod.main(
            [
                "init",
                "--topic",
                "x",
                "--langs",
                "pl",
                "--study",
                path,
                "--since",
                "2009-01",
                "--until",
                "2010-01",
            ]
        )
        == 1
    )
    assert "no complete months" in capsys.readouterr().err
    assert not Path(path).exists(), "a manifest that can never run must not be written"


def test_init_rejects_a_malformed_month_flag_as_a_usage_error(tmp_path, capsys):
    """A non-YYYY-MM --since fails at parse time (exit 2), before any write.

    Verified pre-fix behaviour of resolve_window on such a manifest:
    ``until=banana`` raised an uncaught ValueError (raw traceback -- run.main
    catches only ApiError/SystemExit), and ``since=banana`` produced the
    misleading "requested window banana..2026-08 contains no complete months".
    Both now stop at the flag, with the flag named.
    """
    path = str(tmp_path / "study.json")

    with pytest.raises(SystemExit) as excinfo:
        run_mod.main(
            [
                "init",
                "--topic",
                "x",
                "--langs",
                "pl",
                "--study",
                path,
                "--since",
                "banana",
            ]
        )

    assert excinfo.value.code == 2
    assert "YYYY-MM" in capsys.readouterr().err
    assert not Path(path).exists()


# --- report language (i18n) ---------------------------------------------------
def test_init_stores_and_normalises_the_report_language(tmp_path):
    """The language is a *study* setting: stored once, reused on every rerun."""
    path = str(tmp_path / "study.json")
    assert (
        run_mod.main(
            [*GOLDEN_INIT_ARGS, "--study", path, "--report-lang", " PL "]
        )
        == 0
    )

    assert common.read_json(path)["report_language"] == "pl"


def test_i18n_template_writes_the_reference_then_refuses_to_clobber(
    tmp_path, capsys
):
    out = tmp_path / "translations.pl.json"

    assert run_mod.main(["i18n-template", "--lang", "pl", "--out", str(out)]) == 0
    reference = common.read_json(out)
    assert reference["lang"] == "pl"
    assert set(reference["messages"]) == set(i18n.MESSAGES)
    # The file must teach the agent what to do with it, not just dump strings.
    assert "keep" in reference["_instructions"].lower()

    capsys.readouterr()
    # main() turns the refusal into a visible exit code, not an exception.
    assert (
        run_mod.main(["i18n-template", "--lang", "pl", "--out", str(out)]) == 1
    )
    assert "--force" in capsys.readouterr().err
    # Refusing must not damage the existing (possibly finished) translation.
    assert common.read_json(out) == reference

    assert (
        run_mod.main(
            ["i18n-template", "--lang", "pl", "--out", str(out), "--force"]
        )
        == 0
    )


def test_a_first_localised_run_writes_the_reference_and_says_so(tmp_path, capsys):
    """No translations yet: fall back to English *and* leave the file to edit."""
    path = _init(tmp_path)
    out = tmp_path / "out"
    capsys.readouterr()

    assert (
        run_mod.main(
            ["all", "--study", path, "--out", str(out), "--report-lang", "pl"]
        )
        == 0
    )
    err = capsys.readouterr().err

    # The reference lands where the rerun will look for it.
    assert (out / "translations.pl.json").is_file()
    assert "wrote the English reference file" in err
    # The report still renders -- in English, and it admits that.
    html = (out / "report.html").read_text(encoding="utf-8")
    assert "<html lang=\"en\">" in html
    assert "Untranslated text:" in html


def test_a_translated_rerun_localises_the_report_and_the_chart(tmp_path, capsys):
    """The whole point: edit the reference, rerun, get a localised output."""
    path = _init(tmp_path)
    _set_table_key(path, "[xx]")
    out = tmp_path / "out"
    assert (
        run_mod.main(["all", "--study", path, "--out", str(out), "--report-lang", "xx"])
        == 0
    )
    reference = common.read_json(out / "translations.xx.json")
    reference["messages"] = full_translation("xx")
    common.write_json(out / "translations.xx.json", reference)
    capsys.readouterr()

    assert (
        run_mod.main(["all", "--study", path, "--out", str(out), "--report-lang", "xx"])
        == 0
    )

    html = (out / "report.html").read_text(encoding="utf-8")
    assert '<html lang="xx">' in html
    assert "[xx] Audience interest: intermittent fasting" in html
    assert "Untranslated text:" not in html, "a complete translation shows no note"
    assert "no 'xx' translation" not in capsys.readouterr().err

    # The chart is report-facing text too, so it moves with the same language.
    svg = (out / "chart.svg").read_text(encoding="utf-8")
    assert "[xx] Absolute monthly pageviews" in svg


def test_the_manifest_table_key_reaches_the_report(tmp_path):
    """The decoding under the table comes from study.json, not the catalogue."""
    path = _init(tmp_path)
    _set_table_key(path, "Пояснення:")
    out = tmp_path / "out"

    assert run_mod.main(["all", "--study", path, "--out", str(out)]) == 0

    html = (out / "report.html").read_text(encoding="utf-8")
    assert "<h3>Пояснення: Table key</h3>" in html
    assert "<dt>Share/M</dt><dd>Пояснення: share definition</dd>" in html
    # Metric names never move, whatever the manifest says.
    assert "<th>Share/M</th>" in html
    assert "<h1>Audience interest:" in html, "an English report stays English"
    # A manifest that supplies the whole key needs no note about it.
    assert "Untranslated text" not in html


def test_an_unknown_table_key_entry_is_reported(tmp_path, capsys):
    """A definition for a column the table never prints is work, not silence."""
    path = _init(tmp_path)
    study = common.read_json(path)
    study["table_key"] = {"heading": "Key", "Momentum": "not a column"}
    common.write_json(path, study)
    out = tmp_path / "out"
    capsys.readouterr()

    assert run_mod.main(["all", "--study", path, "--out", str(out)]) == 0

    assert "Momentum" in capsys.readouterr().err


def test_a_localised_report_without_a_manifest_key_admits_the_english_fallback(
    tmp_path, capsys
):
    """Complete translation, no ``table_key``: the key falls back *and* says so."""
    path = _init(tmp_path)
    out = tmp_path / "out"
    out.mkdir()
    common.write_json(
        out / "translations.xx.json",
        {"lang": "xx", "messages": full_translation("xx")},
    )
    capsys.readouterr()

    assert (
        run_mod.main(["all", "--study", path, "--out", str(out), "--report-lang", "xx"])
        == 0
    )

    html = (out / "report.html").read_text(encoding="utf-8")
    assert '<html lang="xx">' in html
    assert "<dt>YoY</dt><dd>Change in article pageviews" in html
    assert "Untranslated text:" in html
    assert "table_key" in capsys.readouterr().err


def test_the_manifest_report_language_applies_without_the_flag(tmp_path):
    """A rerun months later must not need the flag repeated."""
    path = str(tmp_path / "study.json")
    assert (
        run_mod.main(
            [*GOLDEN_INIT_ARGS, "--study", path, "--report-lang", "xx"]
        )
        == 0
    )
    out = tmp_path / "out"
    assert run_mod.main(["all", "--study", path, "--out", str(out)]) == 0
    # The first run left the reference where the manifest's language points.
    reference = out / "translations.xx.json"
    assert reference.is_file()

    payload = common.read_json(reference)
    payload["messages"] = full_translation("xx")
    common.write_json(reference, payload)
    assert run_mod.main(["all", "--study", path, "--out", str(out)]) == 0

    html = (out / "report.html").read_text(encoding="utf-8")
    assert '<html lang="xx">' in html
    assert "[xx] Audience interest: intermittent fasting" in html


def test_a_corrupt_translations_file_stops_the_run(tmp_path, capsys):
    """Silently rendering English instead of the requested language is worse."""
    path = _init(tmp_path)
    out = tmp_path / "out"
    out.mkdir()
    (out / "translations.pl.json").write_text("{not json", encoding="utf-8")

    assert (
        run_mod.main(
            ["all", "--stage", "report", "--study", path, "--out", str(out),
             "--report-lang", "pl"]
        )
        == 1
    )
    assert "cannot read translations" in capsys.readouterr().err


def test_status_surfaces_the_layers_and_the_ranking_criterion(tmp_path, capsys):
    """The step-2 review must show what the study will fetch and how it sorts."""
    path = _init(tmp_path)
    study = common.read_json(path)
    study["layers"] = ["access", "bot"]
    study["criteria"] = {"rank_by": "bot_share"}
    common.write_json(path, study)

    assert run_mod.main(["status", "--study", path]) == 0
    out = capsys.readouterr().out

    assert "layers:    access, bot" in out
    assert "ranking:   bot_share" in out


def test_status_says_nothing_about_a_study_that_asked_for_no_extras(
    tmp_path, capsys
):
    """Absent optional blocks stay absent: no line to misread as a default."""
    path = _init(tmp_path)

    assert run_mod.main(["status", "--study", path]) == 0
    out = capsys.readouterr().out

    assert "layers:" not in out
    assert "ranking:" not in out


def test_report_stage_renders_the_layer_columns_and_the_ranking(tmp_path):
    """End to end through ``run.py``: analysis.json decides the columns.

    The report stage reads no flags for this -- which columns exist follows
    from the metrics that were measured, so a rerun of the stage alone (the
    common "I changed my mind about the wording" loop) stays correct.
    """
    path = _init(tmp_path)
    out = tmp_path / "out"
    assert run_mod.main(["all", "--study", path, "--out", str(out)]) == 0

    analysis = common.read_json(out / "analysis.json")
    for metric in analysis["metrics"].values():
        metric["access_split"] = {
            "total_views": metric["article_total"],
            "desktop_pct": 40.0,
            "mobile_web_pct": 30.0,
            "mobile_app_pct": 30.0,
            "mobile_pct": 60.0,
        }
        metric["bot_share_pct"] = 12.5
        metric["top_rank"] = {"month": "2026-08", "rank": 9, "list_size": 1000}
    analysis["comparison"]["ranked_by"] = {
        "criterion": "bot_share",
        "order": ["cs", "pl"],
    }
    common.write_json(out / "analysis.json", analysis)

    assert (
        run_mod.main(
            ["all", "--study", path, "--out", str(out), "--stage", "report"]
        )
        == 0
    )

    html = (out / "report.html").read_text(encoding="utf-8")
    assert "<th>Mobile %</th>" in html
    assert "<th>Bot %</th>" in html
    assert "<td>60.0%</td>" in html
    assert "<td>12.5%</td>" in html
    assert "<td>9</td>" in html
    assert "Ranked by human traffic (lowest bot share), best first." in html
    assert (out / "report.pdf").is_file()
