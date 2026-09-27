"""Orchestrator: run the whole analysis, or any single stage.

A study is described by a small ``study.json`` manifest so that follow-up
questions ("now add Ukrainian", "use the last 3 years") are a one-field edit
rather than a fresh query -- and the response cache means unchanged steps cost
nothing.

    run.py init --topic "intermittent fasting" --langs pl,cs
    run.py resolve                      # concept -> article per language, then stop
    run.py status                       # resolved? gaps? runner-up concepts
    run.py all --out reports/           # resolve -> fetch -> analyze -> chart -> report
    run.py override --lang pl --title "Post"   # deliberate substitution
    run.py all --out reports/           # rerun; cached responses are reused
    run.py i18n-template --lang pl      # write the message reference to translate

The report and the chart are written in the report language: pass
``--report-lang <code>`` (or set it once with ``init``) and the wording comes
from ``translations.<lang>.json`` beside the results; the English message
reference file is written there automatically when it is missing, so nothing
is ever machine-invented. Data, ``analysis.json`` and the console stay
English either way.

Outputs (study.json is the manifest; series.json, analysis.json, chart.*,
report.* are results) go to --study / --out, defaulting to the current
directory. The HTTP cache lives in <skill>/cache/ and is shared between runs
and studies; --out does not move it.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

import analyze as analyze_mod
import chart as chart_mod
import common
import fetch as fetch_mod
import http_client
import i18n
import report as report_mod
import resolve as resolve_mod
from payloads import (
    AnalysisPayload,
    SeriesPayload,
    StudyManifest,
    StudyWindow,
    load_study_manifest,
)

STAGES = ("resolve", "fetch", "analyze", "chart", "report")

# Subcommands understood by the argument parser. Anything else given first is
# treated as arguments to `all`, so `run.py --out reports/` works as users
# expect instead of failing with "unrecognized arguments".
SUBCOMMANDS = {
    "init",
    "all",
    "resolve",
    "status",
    "override",
    "i18n-template",
    "clear-cache",
}


# --------------------------------------------------------------------------
# Manifest helpers
# --------------------------------------------------------------------------
def _load(study_path: Path) -> StudyManifest:
    """Read and validate the study manifest (the shared boundary check).

    Raises:
        SystemExit: The manifest is missing, unreadable, or fails the schema
            in ``payloads`` -- the message names the offending fields.
    """
    return load_study_manifest(study_path)


def _save(study_path: Path, study: StudyManifest) -> None:
    """Persist the manifest (pretty UTF-8 JSON, parents created)."""
    common.write_json(study_path, study)


def _window_info(study: StudyManifest) -> tuple[str, list[str]]:
    """``(human-readable window, warnings)`` -- always the real dates.

    An unset window means "the last N complete months", which is useless to read
    as ``(default)`` -- resolve it (pure date maths, no network) so ``init`` and
    ``status`` always show the dates the run will actually use, plus anything
    that would silently weaken the study (a clamp, or an unalignable window).

    An *invalid* window is deliberately not caught here: ``resolve_window``
    exits with the real reason, and ``main()`` prints it, so a broken manifest
    surfaces as an error instead of a plausible-looking ``(default)`` label.
    """
    since, until, warnings = fetch_mod.resolve_window(study, date.today())
    return f"{since} .. {until}", warnings


def _print_window(label: str, warnings: list[str], indent: str = "") -> None:
    """Print the resolved window; each warning goes to stderr with indent."""
    print(f"{indent}window:    {label}")
    for warning in warnings:
        print(f"{indent}warning:   {warning}", file=sys.stderr)


def cmd_init(args: argparse.Namespace) -> None:
    """Create a new study manifest after validating the requested window.

    Raises:
        SystemExit: If the manifest exists (without ``--force``) or the
            window is invalid; no file is written in either case.
    """
    study_path = Path(args.study)
    if study_path.exists() and not args.force:
        raise SystemExit(
            f"error: {study_path} already exists (use --force to overwrite)"
        )
    languages = resolve_mod.parse_languages(args.langs)
    window: StudyWindow = {}
    if args.since:
        window["since"] = args.since
    if args.until:
        window["until"] = args.until

    study: StudyManifest = {
        "version": 1,
        "topic": args.topic,
        "languages": languages,
        "window": window,
        "access": common.DEFAULT_ACCESS,
        "agent": common.DEFAULT_AGENT,
        "granularity": "monthly",
        "overrides": {},
        "resolution": None,
    }
    if args.report_lang:
        # The report language is a *study* setting, so a rerun months later
        # still writes the report in the language the study was asked for.
        study["report_language"] = i18n.normalize_lang(args.report_lang)
    # Resolve (and thereby validate) the window *before* writing anything: a
    # rejected window must leave no manifest behind to confuse the next run.
    label, warnings = _window_info(study)
    _save(study_path, study)
    print(f"wrote {study_path}")
    print(f"  topic:     {args.topic}")
    print(f"  languages: {', '.join(languages)}")
    if study.get("report_language"):
        print(f"  report:    {study['report_language']}")
    else:
        print(
            "  report:    (not set) -- pass --report-lang <the language the "
            "user prompted in>"
        )
    _print_window(label, warnings, indent="  ")
    if not window:
        print(
            f"             (default: last {fetch_mod.DEFAULT_MONTHS} complete months)"
        )
    print(f"Next: run.py resolve --study {study_path}")
    print(f"Then: run.py all --study {study_path} --out <dir>")


# --------------------------------------------------------------------------
# Stages
# --------------------------------------------------------------------------
def stage_resolve(
    study: StudyManifest,
    args: argparse.Namespace,
    *,
    client: common.JsonFetcher,
) -> StudyManifest:
    """Resolve the concept (Wikidata + per-language sitelinks) into the manifest.

    Prints every article found and every gap with its candidates, then stores
    the resolution in the manifest (the caller saves it).

    Args:
        study: Loaded manifest; ``resolution`` is written in place.
        args: Parsed CLI args (uses ``qid`` and ``search_lang``).
        client: The run's shared transport (see :func:`main`).

    Returns:
        The same manifest dict, now carrying ``resolution``.

    Raises:
        ApiError: If Wikidata/Wikipedia cannot be searched (see ``resolve``).
    """
    resolution = resolve_mod.resolve(
        study["topic"],
        study["languages"],
        client=client,
        qid=args.qid,
        search_language=args.search_lang,
    )
    study["resolution"] = resolution

    print(f"  concept:   {resolution['qid']} '{resolution['label']}'")
    for language, article in resolution["articles"].items():
        if article:
            print(f"  {language}: {article['title']}")
        else:
            print(f"  {language}: GAP -- no article for this topic")
            for candidate in resolution["candidates"].get(language, []):
                print(
                    f"      candidate: {candidate['title']}  "
                    f"({common.clip(candidate['snippet'])})"
                )
            print(
                f"      next: ask the user to pick one of these candidates, "
                f"or to skip {language}"
            )
            print(
                f"      to analyse one: run.py override --lang {language} "
                f'--title "<candidate>"'
            )
    for warning in resolution["warnings"]:
        print(f"  warning: {warning}", file=sys.stderr)
    return study


def stage_fetch(
    study: StudyManifest,
    out_dir: Path,
    args: argparse.Namespace,
    *,
    client: common.JsonFetcher,
) -> SeriesPayload:
    """Fetch both pageview series per language and write ``series.json``.

    Args:
        study: Manifest carrying a completed ``resolution``.
        out_dir: Directory for the artifact (created by ``cmd_run``).
        args: Parsed CLI args (unused here; kept for stage uniformity).
        client: The run's shared transport (see :func:`main`).

    Returns:
        The :class:`payloads.SeriesPayload` that was written.

    Raises:
        SystemExit: When the study has not been resolved yet.
        ApiError: For API failures beyond the retry budget.
    """
    if not study.get("resolution"):
        raise SystemExit(
            "error: study has no resolution yet -- run 'run.py resolve' first"
        )
    payload = fetch_mod.fetch_series(study, client=client)
    path = out_dir / "series.json"
    common.write_json(path, payload)
    for language, item in payload["series"].items():
        print(
            f"  {language}: {item['article_title']} -- {len(item['labels'])} months, "
            f"{sum(item['article_views']):,} views"
        )
    for language in payload["gaps"]:
        print(f"  {language}: GAP (no article to measure)")
    for warning in payload["warnings"]:
        print(f"  warning: {warning}", file=sys.stderr)
    print(f"  wrote {path}")
    return payload


def stage_analyze(out_dir: Path) -> AnalysisPayload:
    """Compute metrics from ``series.json`` and write ``analysis.json``.

    Args:
        out_dir: Directory holding the artifacts.

    Returns:
        The :class:`payloads.AnalysisPayload` that was written.

    Raises:
        SystemExit: When ``series.json`` is missing (fetch has not run).
    """
    series_path = out_dir / "series.json"
    if not series_path.is_file():
        raise SystemExit(
            f"error: {series_path} not found -- run 'run.py all --stage fetch' first"
        )
    analysis = analyze_mod.analyze(common.read_json(series_path))
    path = out_dir / "analysis.json"
    common.write_json(path, analysis)
    print(f"  wrote {path}")
    print(f"  {analysis['headline']}")
    return analysis


def stage_chart(
    analysis: AnalysisPayload,
    out_dir: Path,
    translator: i18n.Translator | None = None,
) -> dict[str, str]:
    """Render the comparison chart as PNG and SVG inside ``out_dir``."""
    paths = chart_mod.render(analysis, out_dir / "chart", translator)
    print(f"  wrote {paths['png']}")
    print(f"  wrote {paths['svg']}")
    return paths


def stage_report(
    analysis: AnalysisPayload,
    out_dir: Path,
    translator: i18n.Translator | None = None,
    *,
    table_key: dict[str, str] | None = None,
) -> None:
    """Write ``report.html`` always and ``report.pdf`` when it fits one page.

    Args:
        analysis: The ``analysis.json`` payload.
        out_dir: Where both artifacts land.
        translator: Report language; English when omitted.
        table_key: The manifest's ``table_key`` block -- the generated (never
            translated) decoding printed under the comparison table.

    Raises:
        SystemExit: Code 1 when the PDF would overflow: the HTML is kept,
            the partial PDF is deleted, and the reason is printed.
    """
    chart_png = out_dir / "chart.png"
    html_path = out_dir / "report.html"
    pdf_path = out_dir / "report.pdf"
    report_mod.render_html(
        analysis,
        chart_png if chart_png.is_file() else None,
        html_path,
        translator,
        table_key=table_key,
    )
    print(f"  wrote {html_path}")
    try:
        report_mod.render_pdf(
            analysis,
            chart_png if chart_png.is_file() else None,
            pdf_path,
            translator,
            table_key=table_key,
        )
    except report_mod.ReportOverflow as exc:
        print(f"  error: {exc}", file=sys.stderr)
        print("  The HTML report was written; the PDF was not.", file=sys.stderr)
        if pdf_path.exists():
            pdf_path.unlink()
        raise SystemExit(1) from exc
    print(f"  wrote {pdf_path} (1 page)")


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------
def cmd_status(args: argparse.Namespace) -> None:
    """Print the manifest: topic, languages, window, resolution, candidates."""
    study = _load(Path(args.study))
    print(f"topic:     {study['topic']}")
    print(f"languages: {', '.join(study['languages'])}")
    if study.get("report_language"):
        print(f"report:    {study['report_language']}")
    else:
        print(
            "report:    (not set) -- pass --report-lang <the language the "
            "user prompted in>"
        )
    if study.get("table_key"):
        print(f"table key: {len(study['table_key'])} entries")
    label, warnings = _window_info(study)
    _print_window(label, warnings)
    if study.get("overrides"):
        for language, title in study["overrides"].items():
            print(f"override:  {language} = {title}")
    resolution = study.get("resolution")
    if not resolution:
        print("resolution: not resolved yet (run 'run.py resolve')")
        return
    print(f"concept:   {resolution['qid']} '{resolution['label']}'")
    if resolution.get("description"):
        print(f"           {resolution['description']}")
    # The runner-up concepts are exactly what you need when the wrong Q-item was
    # picked, which is the whole reason step 2 of the workflow exists.
    hits = resolution.get("search_hits") or []
    if hits:
        print("hits:      (review these; re-run resolve with --qid to pick another)")
        for hit in hits:
            marker = "->" if hit["id"] == resolution["qid"] else "  "
            label = hit.get("label") or "(no label)"
            description = hit.get("description") or ""
            print(f"  {marker} {hit['id']}  {label}  {description}")
    for language, article in resolution["articles"].items():
        if article:
            print(f"  {language}: {article['title']}")
        else:
            print(f"  {language}: GAP")
            for candidate in resolution["candidates"].get(language, []):
                print(f"      candidate: {candidate['title']}")
            print(
                f"      next: ask the user to pick one of these candidates, "
                f"or to skip {language}"
            )


def cmd_override(args: argparse.Namespace) -> None:
    """Record a deliberate article substitution in the manifest.

    Raises:
        SystemExit: When ``--lang`` is not one of the study's languages.
    """
    study_path = Path(args.study)
    study = _load(study_path)
    if args.lang not in study["languages"]:
        raise SystemExit(
            f"error: {args.lang!r} is not one of this study's languages "
            f"({', '.join(study['languages'])})"
        )
    study.setdefault("overrides", {})[args.lang] = args.title
    _save(study_path, study)
    print(f"override set: {args.lang} = {args.title}")
    print("This is an explicit substitution -- record why in your report.")


def cmd_clear_cache(args: argparse.Namespace) -> None:
    """Delete every cached API response and report how many were removed."""
    removed = common.clear_cache()
    print(f"removed {removed} cached response(s) from {common.CACHE_DIR}")


def cmd_i18n_template(args: argparse.Namespace) -> None:
    """Write the English message reference file the agent translates in place.

    Raises:
        SystemExit: When the file exists and ``--force`` was not given --
            overwriting a finished translation would silently lose it.
    """
    lang = i18n.normalize_lang(args.lang)
    path = Path(args.out) if args.out else Path(f"translations.{lang}.json")
    i18n.write_reference(path, lang, force=args.force)
    print(f"wrote {path} ({len(i18n.MESSAGES)} messages)")
    print('Translate every value in "messages" into the target language; keep')
    print("the message ids and the {placeholder} names exactly as they are.")
    print(
        f"Then: run.py all --study <study.json> --out <dir> --report-lang {lang}"
    )


def cmd_run(args: argparse.Namespace, *, client: common.JsonFetcher) -> None:
    """Run one stage (``--stage``) or the whole pipeline in order.

    Each stage prints what it wrote; resolve results are saved back into the
    manifest, while everything else lands in ``--out``. Failures stop the run
    and are reported by :func:`main`.

    Args:
        args: Parsed CLI args (``study``, ``out``, ``stage``, ``no_cache``).
        client: The run's shared transport -- built once in :func:`main` and
            passed to every networked stage, so one politeness cadence spans
            the whole run (``--no-cache`` is already applied to it).
    """
    study_path = Path(args.study)
    # `resolve` writes nothing outside the manifest, so it has no --out flag.
    out_dir = Path(getattr(args, "out", "."))
    out_dir.mkdir(parents=True, exist_ok=True)

    stages = list(STAGES) if args.stage == "all" else [args.stage]
    study = _load(study_path)
    analysis: AnalysisPayload | None = None
    # The table key is generated into the manifest, never translated, so it
    # is read once here and handed straight to the report stage.
    table_key = study.get("table_key")
    if "report" in stages:
        report_mod.warn_unknown_table_key(table_key)

    # The report language only matters to the two stages that write
    # human-facing artifacts, and both share one translator so the fallbacks
    # recorded for the chart are still visible in the report's note.
    translator: i18n.Translator | None = None
    if {"chart", "report"} & set(stages):
        lang = i18n.normalize_lang(
            getattr(args, "report_lang", "") or study.get("report_language")
        )
        explicit = getattr(args, "translations", "")
        translations = (
            Path(explicit) if explicit else out_dir / f"translations.{lang}.json"
        )
        translator = i18n.translator_for(lang, translations)

    try:
        for stage in stages:
            print(f"[{stage}]")
            if stage == "resolve":
                study = stage_resolve(study, args, client=client)
                _save(study_path, study)
            elif stage == "fetch":
                stage_fetch(study, out_dir, args, client=client)
            elif stage == "analyze":
                analysis = stage_analyze(out_dir)
            elif stage == "chart":
                analysis = analysis or common.read_json(out_dir / "analysis.json")
                stage_chart(analysis, out_dir, translator)
            elif stage == "report":
                analysis = analysis or common.read_json(out_dir / "analysis.json")
                stage_report(analysis, out_dir, translator, table_key=table_key)
    finally:
        # Even when a stage failed, the caller must learn that the output is
        # (partly) English rather than assume the requested language landed.
        if translator is not None:
            i18n.warn_untranslated(translator)

    if analysis is None and "analyze" in stages:
        return
    if analysis is not None:
        print("[done]")
        print(f"  {analysis['headline']}")
        print(f"  report: {out_dir / 'report.pdf'}")
        print(f"  html:   {out_dir / 'report.html'}")
        print(f"  data:   {out_dir / 'analysis.json'}")


# --------------------------------------------------------------------------
def _month_arg(value: str) -> str:
    """argparse ``type=`` for ``--since``/``--until``: only ``YYYY-MM`` passes.

    Raises:
        argparse.ArgumentTypeError: The value is malformed -- argparse then
            prints the reason and exits with code 2 before anything is
            written, instead of a later stage crashing on the bad month.
    """
    try:
        return common.parse_month(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def build_parser() -> argparse.ArgumentParser:
    """Build the ``run.py`` CLI: one subcommand per action, ``all`` by default."""
    parser = argparse.ArgumentParser(
        prog="run.py",
        description="Wikipedia audience-interest analysis (resolve -> fetch -> "
        "analyze -> chart -> report).",
    )
    sub = parser.add_subparsers(dest="command")

    def common_args(target: argparse.ArgumentParser) -> None:
        target.add_argument("--study", default="study.json", help="study manifest path")
        target.add_argument(
            "--no-cache",
            action="store_true",
            help="ignore cached responses (forces refetch)",
        )

    init = sub.add_parser("init", help="create a study manifest")
    common_args(init)
    init.add_argument("--topic", required=True, help="topic to analyse")
    init.add_argument(
        "--langs", required=True, help="comma-separated language codes, e.g. pl,cs"
    )
    init.add_argument("--since", type=_month_arg, help="window start, YYYY-MM")
    init.add_argument(
        "--until",
        type=_month_arg,
        help="window end, YYYY-MM (must be a complete month)",
    )
    init.add_argument(
        "--force", action="store_true", help="overwrite an existing manifest"
    )
    init.add_argument(
        "--report-lang",
        default="",
        help="language for the report and chart text, e.g. pl (default: English)",
    )
    init.set_defaults(func=cmd_init)

    run = sub.add_parser("all", help="run every stage (default)")
    common_args(run)
    run.add_argument(
        "--stage", default="all", choices=("all",) + STAGES, help="run a single stage"
    )
    run.add_argument("--out", default=".", help="output directory (default: cwd)")
    run.add_argument("--qid", help="skip topic search; use this Wikidata id")
    run.add_argument("--search-lang", default="en", help="language for concept search")
    run.add_argument(
        "--report-lang",
        default="",
        help="report/chart language, e.g. pl; falls back to the study's "
        "report_language, then English",
    )
    run.add_argument(
        "--translations",
        default="",
        help="translations JSON (default: <out>/translations.<lang>.json)",
    )
    run.set_defaults(func=cmd_run)

    # `resolve` on its own so the resolution can be *reviewed* (right concept?
    # right articles? gaps?) before any pageviews are fetched. Referenced by
    # `status`, `stage_fetch` and SKILL.md, so it must really exist.
    res = sub.add_parser(
        "resolve", help="resolve the topic to an article per language, then stop"
    )
    common_args(res)
    res.add_argument("--qid", help="skip topic search; use this Wikidata id")
    res.add_argument("--search-lang", default="en", help="language for concept search")
    res.set_defaults(func=cmd_run, stage="resolve")

    status = sub.add_parser("status", help="show what is resolved and what is a gap")
    common_args(status)
    status.set_defaults(func=cmd_status)

    override = sub.add_parser(
        "override", help="explicitly choose an article for a gap language"
    )
    common_args(override)
    override.add_argument("--lang", required=True, help="language code")
    override.add_argument("--title", required=True, help="exact article title")
    override.set_defaults(func=cmd_override)

    clear = sub.add_parser("clear-cache", help="delete cached API responses")
    clear.set_defaults(func=cmd_clear_cache)

    # The English message reference the agent translates: run it once before
    # the first `--report-lang` run, then edit the file it writes.
    template = sub.add_parser(
        "i18n-template",
        help="write the English message reference file to translate",
    )
    template.add_argument("--lang", required=True, help="language code, e.g. pl")
    template.add_argument(
        "--out",
        default="",
        help="output path (default: translations.<lang>.json)",
    )
    template.add_argument(
        "--force", action="store_true", help="overwrite an existing file"
    )
    template.set_defaults(func=cmd_i18n_template)

    return parser


def main(argv: list[str] | None = None) -> int:
    """Dispatch a subcommand and turn every failure into a visible exit code.

    A missing subcommand means ``all``; leading flags belong to ``all``.
    Networked commands (:func:`cmd_run`) get one freshly built
    ``http_client.default_client()`` -- the composition root of this CLI.

    Returns:
        ``0`` on success, else the failing stage's exit code (``1`` for
        errors). Error messages from stages (``SystemExit("error: ...")``)
        are printed to stderr here -- swallowing them would make every CLI
        failure silent.
    """
    common.configure_console()
    parser = build_parser()

    # No subcommand -> run everything; leading flags belong to `all`.
    words = list(sys.argv[1:] if argv is None else argv)
    if not words:
        words = ["all"]
    elif words[0] not in SUBCOMMANDS and words[0] not in ("-h", "--help"):
        words = ["all", *words]

    args = parser.parse_args(words)
    try:
        if args.func is cmd_run:
            # The client is built here, not in the stages: one session, one
            # cache switch, one politeness cadence for the whole run.
            # Commands that never talk to the API never construct one.
            with http_client.default_client(cache_enabled=not args.no_cache) as client:
                args.func(args, client=client)
        else:
            args.func(args)
    except common.ApiError as exc:
        print(f"error [{exc.kind}]: {exc}", file=sys.stderr)
        return 1
    except SystemExit as exc:
        if isinstance(exc.code, int):
            return exc.code
        if exc.code is not None:
            # SystemExit("error: ...") carries the message as its code.
            # Printing it here is what makes `run.py` failures visible --
            # swallowing it would exit 1 with no explanation at all.
            print(exc.code, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
