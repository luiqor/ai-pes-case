"""Orchestrator: run the whole analysis, or any single stage.

A study is described by a small ``study.json`` manifest so that follow-up
questions ("now add Ukrainian", "use the last 3 years") are a one-field edit
rather than a fresh query -- and the response cache means unchanged steps cost
nothing.

    run.py init --topic "intermittent fasting" --langs pl,cs
    run.py resolve                      # concept -> article per language, then stop
    run.py status                       # what is resolved, what is a gap, which candidates
    run.py all --out reports/           # resolve -> fetch -> analyze -> chart -> report
    run.py override --lang pl --title "Post"   # deliberate substitution, only ever explicit
    run.py all --out reports/           # rerun; cached responses are reused

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
from typing import Any, Callable

import analyze as analyze_mod
import chart as chart_mod
import common
import fetch as fetch_mod
import report as report_mod
import resolve as resolve_mod

STAGES = ("resolve", "fetch", "analyze", "chart", "report")

# Subcommands understood by the argument parser. Anything else given first is
# treated as arguments to `all`, so `run.py --out reports/` works as users
# expect instead of failing with "unrecognized arguments".
SUBCOMMANDS = {"init", "all", "resolve", "status", "override", "clear-cache"}


# --------------------------------------------------------------------------
# Manifest helpers
# --------------------------------------------------------------------------
def _load(study_path: Path) -> dict[str, Any]:
    if not study_path.is_file():
        raise SystemExit(
            f"error: no study manifest at {study_path}\n"
            'Create one with:  run.py init --topic "..." --langs pl,cs'
        )
    return common.read_json(study_path)


def _save(study_path: Path, study: dict[str, Any]) -> None:
    common.write_json(study_path, study)


def _window_info(study: dict[str, Any]) -> tuple[str, list[str]]:
    """``(human-readable window, warnings)`` -- always the real dates.

    An unset window means "the last N complete months", which is useless to read
    as ``(default)`` -- resolve it (pure date maths, no network) so ``init`` and
    ``status`` always show the dates the run will actually use, plus anything
    that would silently weaken the study (a clamp, or an unalignable window).
    """
    window = study.get("window") or {}
    try:
        since, until, warnings = fetch_mod.resolve_window(study, date.today())
    except SystemExit:
        return (
            f"{window.get('since', '(default)')} .. "
            f"{window.get('until', '(default)')}",
            [],
        )
    return f"{since} .. {until}", warnings


def _print_window(study: dict[str, Any], indent: str = "") -> None:
    label, warnings = _window_info(study)
    print(f"{indent}window:    {label}")
    for warning in warnings:
        print(f"{indent}warning:   {warning}", file=sys.stderr)


def cmd_init(args: argparse.Namespace) -> None:
    study_path = Path(args.study)
    if study_path.exists() and not args.force:
        raise SystemExit(
            f"error: {study_path} already exists (use --force to overwrite)"
        )
    languages = resolve_mod.parse_languages(args.langs)
    window: dict[str, str] = {}
    if args.since:
        window["since"] = args.since
    if args.until:
        window["until"] = args.until

    study = {
        "version": 1,
        "topic": args.topic,
        "languages": languages,
        "window": window or {},
        "access": common.DEFAULT_ACCESS,
        "agent": common.DEFAULT_AGENT,
        "granularity": "monthly",
        "overrides": {},
        "resolution": None,
    }
    _save(study_path, study)
    print(f"wrote {study_path}")
    print(f"  topic:     {args.topic}")
    print(f"  languages: {', '.join(languages)}")
    _print_window(study, indent="  ")
    if not window:
        print(f"             (default: last {fetch_mod.DEFAULT_MONTHS} complete months)")
    print(f"Next: run.py resolve --study {study_path}")
    print(f"Then: run.py all --study {study_path} --out <dir>")


# --------------------------------------------------------------------------
# Stages
# --------------------------------------------------------------------------
def stage_resolve(study: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    resolution = resolve_mod.resolve(
        study["topic"],
        study["languages"],
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
                f'      to analyse one: run.py override --lang {language} '
                f'--title "<candidate>"'
            )
    for warning in resolution["warnings"]:
        print(f"  warning: {warning}", file=sys.stderr)
    return study


def stage_fetch(study: dict[str, Any], out_dir: Path, args: argparse.Namespace) -> dict[str, Any]:
    if not study.get("resolution"):
        raise SystemExit("error: study has no resolution yet -- run 'run.py resolve' first")
    payload = fetch_mod.fetch_series(study)
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


def stage_analyze(out_dir: Path) -> dict[str, Any]:
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


def stage_chart(analysis: dict[str, Any], out_dir: Path) -> dict[str, str]:
    paths = chart_mod.render(analysis, out_dir / "chart")
    print(f"  wrote {paths['png']}")
    print(f"  wrote {paths['svg']}")
    return paths


def stage_report(analysis: dict[str, Any], out_dir: Path) -> None:
    chart_png = out_dir / "chart.png"
    html_path = out_dir / "report.html"
    pdf_path = out_dir / "report.pdf"
    report_mod.render_html(analysis, chart_png if chart_png.is_file() else None, html_path)
    print(f"  wrote {html_path}")
    try:
        report_mod.render_pdf(analysis, chart_png if chart_png.is_file() else None, pdf_path)
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
    study = _load(Path(args.study))
    print(f"topic:     {study['topic']}")
    print(f"languages: {', '.join(study['languages'])}")
    _print_window(study)
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


def cmd_override(args: argparse.Namespace) -> None:
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
    removed = common.clear_cache()
    print(f"removed {removed} cached response(s) from {common.CACHE_DIR}")


def cmd_run(args: argparse.Namespace) -> None:
    study_path = Path(args.study)
    # `resolve` writes nothing outside the manifest, so it has no --out flag.
    out_dir = Path(getattr(args, "out", "."))
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.no_cache:
        common.set_cache_enabled(False)

    stages = list(STAGES) if args.stage == "all" else [args.stage]
    study = _load(study_path)
    analysis: dict[str, Any] | None = None

    for stage in stages:
        print(f"[{stage}]")
        if stage == "resolve":
            study = stage_resolve(study, args)
            _save(study_path, study)
        elif stage == "fetch":
            stage_fetch(study, out_dir, args)
        elif stage == "analyze":
            analysis = stage_analyze(out_dir)
        elif stage == "chart":
            analysis = analysis or common.read_json(out_dir / "analysis.json")
            stage_chart(analysis, out_dir)
        elif stage == "report":
            analysis = analysis or common.read_json(out_dir / "analysis.json")
            stage_report(analysis, out_dir)

    if analysis is None and "analyze" in stages:
        return
    if analysis is not None:
        print("[done]")
        print(f"  {analysis['headline']}")
        print(f"  report: {out_dir / 'report.pdf'}")
        print(f"  html:   {out_dir / 'report.html'}")
        print(f"  data:   {out_dir / 'analysis.json'}")


# --------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run.py",
        description="Wikipedia audience-interest analysis (resolve -> fetch -> "
        "analyze -> chart -> report).",
    )
    sub = parser.add_subparsers(dest="command")

    def common_args(target: argparse.ArgumentParser) -> None:
        target.add_argument("--study", default="study.json", help="study manifest path")
        target.add_argument("--no-cache", action="store_true",
                            help="ignore cached responses (forces refetch)")

    init = sub.add_parser("init", help="create a study manifest")
    common_args(init)
    init.add_argument("--topic", required=True, help="topic to analyse")
    init.add_argument("--langs", required=True, help="comma-separated language codes, e.g. pl,cs")
    init.add_argument("--since", help="window start, YYYY-MM")
    init.add_argument("--until", help="window end, YYYY-MM (must be a complete month)")
    init.add_argument("--force", action="store_true", help="overwrite an existing manifest")
    init.set_defaults(func=cmd_init)

    run = sub.add_parser("all", help="run every stage (default)")
    common_args(run)
    run.add_argument("--stage", default="all",
                     choices=("all",) + STAGES, help="run a single stage")
    run.add_argument("--out", default=".", help="output directory (default: cwd)")
    run.add_argument("--qid", help="skip topic search; use this Wikidata id")
    run.add_argument("--search-lang", default="en", help="language for concept search")
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

    override = sub.add_parser("override", help="explicitly choose an article for a gap language")
    common_args(override)
    override.add_argument("--lang", required=True, help="language code")
    override.add_argument("--title", required=True, help="exact article title")
    override.set_defaults(func=cmd_override)

    clear = sub.add_parser("clear-cache", help="delete cached API responses")
    clear.set_defaults(func=cmd_clear_cache)

    return parser


def main(argv: list[str] | None = None) -> int:
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
        args.func(args)
    except common.ApiError as exc:
        print(f"error [{exc.kind}]: {exc}", file=sys.stderr)
        return 1
    except SystemExit as exc:
        if isinstance(exc.code, int):
            return exc.code
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
