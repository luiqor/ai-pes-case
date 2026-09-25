"""Render the comparison chart (PNG + SVG) from ``analysis.json``.

Two stacked panels, because the two metrics answer different questions:

* **absolute monthly views** -- how many readers the article actually gets;
* **share per million project views** -- whether the topic is gaining or losing
  ground *relative to the whole language edition*, which is what makes articles
  on differently-sized wikis (pl ~3x cs) comparable at all.

Output is deterministic: fixed figure size, fixed dpi, no randomness, and the
non-interactive ``Agg`` backend so it also works over SSH or in CI.

Panel titles, axis labels and the gap note come from :mod:`i18n`, so passing a
``translator`` renders the chart's own words in the report language (the
plotted data -- series values, article titles, month labels -- is never
translated).
"""

from __future__ import annotations

import argparse
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import common  # noqa: E402
import i18n  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402  (backend must be set first)
from payloads import AnalysisPayload  # noqa: E402

FIGSIZE = (10, 5)
DPI = 150
# Distinct, colour-blind-safe ordering; extras fall back to the default cycle.
COLOURS = ["#1f77b4", "#d62728", "#2ca02c", "#9467bd", "#ff7f0e", "#17becf"]


def _tick_indices(count: int, max_ticks: int = 9) -> list[int]:
    """Indices of the x-axis labels to show, evenly spaced across the window."""
    if count <= max_ticks:
        return list(range(count))
    step = max(1, count // max_ticks)
    return list(range(0, count, step))


def render(
    analysis: AnalysisPayload,
    out_prefix: Path,
    translator: i18n.Translator | None = None,
) -> dict[str, str]:
    """Write ``<prefix>.png`` and ``<prefix>.svg``.

    Args:
        analysis: ``analysis.json`` content; languages without a series
            (coverage gaps) are skipped, and a fully empty set is an error.
        out_prefix: Output path without extension; parents are created.
        translator: Target language for the chart's own labels; English when
            omitted.

    Returns:
        ``{"png": <path>, "svg": <path>}``.

    Raises:
        SystemExit: When every requested language is a coverage gap, so
            there is nothing honest to plot.
    """
    tr = translator or i18n.english()
    metrics = analysis["metrics"]
    languages = [
        lang for lang, m in metrics.items() if m.get("series") and m["series"]["labels"]
    ]
    if not languages:
        raise SystemExit(
            "error: no language series to chart -- every requested language was "
            "a coverage gap. Pick candidates (overrides.<lang>) and re-run."
        )

    fig, (ax_views, ax_share) = plt.subplots(2, 1, figsize=FIGSIZE, sharex=True)

    for index, language in enumerate(languages):
        series = metrics[language]["series"]
        labels = series["labels"]
        x = list(range(len(labels)))
        colour = COLOURS[index % len(COLOURS)]
        name = tr.t(
            "chart.legend_entry",
            lang=language,
            title=metrics[language]["article_title"],
        )

        ax_views.plot(
            x,
            series["article_views"],
            marker="o",
            markersize=3,
            linewidth=1.6,
            color=colour,
            label=name,
        )
        ax_share.plot(
            x,
            series["share_ppm"],
            marker="o",
            markersize=3,
            linewidth=1.6,
            color=colour,
            label=name,
        )

    ax_views.set_ylabel(tr.t("chart.views_ylabel"))
    ax_views.set_title(tr.t("chart.views_title"))
    ax_share.set_ylabel(tr.t("chart.share_ylabel"))
    ax_share.set_title(tr.t("chart.share_title"))
    ax_share.set_xlabel(tr.t("chart.xlabel"))

    window = analysis["window"]
    months = window.get("months", len(metrics[languages[0]]["series"]["labels"]))
    fig.suptitle(
        tr.t(
            "chart.suptitle",
            topic=analysis["topic"],
            since=window["since"],
            until=window["until"],
            months=months,
        ),
        fontsize=12,
    )
    if analysis.get("gaps"):
        fig.text(
            0.5,
            0.005,
            tr.t("chart.gaps_note", langs=tr.t("join.comma").join(analysis["gaps"])),
            ha="center",
            fontsize=8,
            style="italic",
            color="#666666",
        )

    labels = metrics[languages[0]]["series"]["labels"]
    indices = _tick_indices(len(labels))
    ax_share.set_xticks(indices)
    ax_share.set_xticklabels(
        [labels[i] for i in indices], rotation=45, ha="right", fontsize=8
    )
    for axis in (ax_views, ax_share):
        axis.grid(True, alpha=0.3, linewidth=0.6)
        axis.legend(fontsize=8, loc="upper right", framealpha=0.9)

    fig.tight_layout(rect=(0, 0.03, 1, 0.95))

    out_prefix.parent.mkdir(parents=True, exist_ok=True)
    png = out_prefix.with_suffix(".png")
    svg = out_prefix.with_suffix(".svg")
    fig.savefig(png, dpi=DPI)
    fig.savefig(svg)
    plt.close(fig)

    return {"png": str(png), "svg": str(svg)}


def build_parser() -> argparse.ArgumentParser:
    """Build the ``chart.py`` command-line parser."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--analysis", default="analysis.json", help="input from analyze.py"
    )
    parser.add_argument(
        "--out", default="chart", help="output path prefix (no extension)"
    )
    parser.add_argument(
        "--report-lang",
        default="",
        help="language for the chart's labels (default: English), e.g. pl",
    )
    parser.add_argument(
        "--translations",
        default="",
        help="translations JSON (default: translations.<lang>.json)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Read ``analysis.json`` and write both chart files."""
    common.configure_console()
    args = build_parser().parse_args(argv)
    analysis = common.read_json(args.analysis)
    lang = i18n.normalize_lang(args.report_lang)
    fallback = Path(f"translations.{lang}.json")
    translations = Path(args.translations) if args.translations else fallback
    translator = i18n.translator_for(lang, translations)
    paths = render(analysis, Path(args.out), translator)
    for path in paths.values():
        print(f"wrote {path}")
    i18n.warn_untranslated(translator)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
