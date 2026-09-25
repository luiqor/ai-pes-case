"""Render the comparison chart (PNG + SVG) from ``analysis.json``.

Two stacked panels, because the two metrics answer different questions:

* **absolute monthly views** -- how many readers the article actually gets;
* **share per million project views** -- whether the topic is gaining or losing
  ground *relative to the whole language edition*, which is what makes articles
  on differently-sized wikis (pl ~3x cs) comparable at all.

Output is deterministic: fixed figure size, fixed dpi, no randomness, and the
non-interactive ``Agg`` backend so it also works over SSH or in CI.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402  (backend must be set first)

import common  # noqa: E402

FIGSIZE = (10, 5)
DPI = 150
# Distinct, colour-blind-safe ordering; extras fall back to the default cycle.
COLOURS = ["#1f77b4", "#d62728", "#2ca02c", "#9467bd", "#ff7f0e", "#17becf"]


def _tick_indices(count: int, max_ticks: int = 9) -> list[int]:
    if count <= max_ticks:
        return list(range(count))
    step = max(1, count // max_ticks)
    return list(range(0, count, step))


def render(analysis: dict[str, Any], out_prefix: Path) -> dict[str, str]:
    """Write ``<prefix>.png`` and ``<prefix>.svg``. Returns the paths."""
    metrics = analysis["metrics"]
    languages = [
        lang
        for lang, m in metrics.items()
        if m.get("series") and m["series"]["labels"]
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
        name = f"{language}: {metrics[language]['article_title']}"

        ax_views.plot(x, series["article_views"], marker="o", markersize=3,
                      linewidth=1.6, color=colour, label=name)
        ax_share.plot(x, series["share_ppm"], marker="o", markersize=3,
                      linewidth=1.6, color=colour, label=name)

    ax_views.set_ylabel("views / month")
    ax_views.set_title("Absolute monthly pageviews")
    ax_share.set_ylabel("per million project views")
    ax_share.set_title("Normalised share of all wiki pageviews")
    ax_share.set_xlabel("month")

    window = analysis["window"]
    fig.suptitle(
        f"{analysis['topic']}  ({window['since']} .. {window['until']}, "
        f"{window.get('months', len(metrics[languages[0]]['series']['labels']))} months)",
        fontsize=12,
    )
    if analysis.get("gaps"):
        fig.text(
            0.5,
            0.005,
            "no article in: " + ", ".join(analysis["gaps"]),
            ha="center",
            fontsize=8,
            style="italic",
            color="#666666",
        )

    labels = metrics[languages[0]]["series"]["labels"]
    indices = _tick_indices(len(labels))
    ax_share.set_xticks(indices)
    ax_share.set_xticklabels([labels[i] for i in indices], rotation=45, ha="right",
                             fontsize=8)
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
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--analysis", default="analysis.json", help="input from analyze.py")
    parser.add_argument("--out", default="chart", help="output path prefix (no extension)")
    return parser


def main(argv: list[str] | None = None) -> int:
    common.configure_console()
    args = build_parser().parse_args(argv)
    analysis = common.read_json(args.analysis)
    paths = render(analysis, Path(args.out))
    for kind, path in paths.items():
        print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
