"""Produce the shareable report: a self-contained HTML file and a one-page PDF.

The PDF is **strictly one page**: layout is driven by an explicit vertical
cursor, and every block asks for the space it needs before drawing. If the
content does not fit, a ``ReportOverflow`` is raised and *no PDF is written* --
the caller is told which block overflowed instead of silently emitting a
two-page document.

Charts and typography use fonts shipped with the pinned matplotlib dependency
(DejaVu), so accented titles such as ``Přerušovaný půst`` render correctly
without depending on fonts installed on the host system.

Every string the reader sees comes from :mod:`i18n`: pass a ``translator``
built for the requested report language and the report is rendered in that
language, with anything untranslated falling back to English *and* saying so
in a visible note (never silently).
"""

from __future__ import annotations

import argparse
import base64
import html
import string
import sys
from pathlib import Path

import common
import i18n
from payloads import AnalysisPayload, MessageRef
from reportlab.lib.colors import Color, HexColor
from reportlab.lib.pagesizes import letter
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.pdfdoc import PDFError
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas as pdf_canvas

SKILL_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_PATH = SKILL_ROOT / "assets" / "report_template.html"

PAGE_W, PAGE_H = letter
MARGIN = 40
CONTENT_W = PAGE_W - 2 * MARGIN
BOTTOM = MARGIN

ACCENT = HexColor(common.ACCENT_RED)
MUTED = HexColor("#666666")
RULE = HexColor("#d9d9d9")
INK = HexColor("#111111")

# Swiss grid ornament: every major section is announced by its number, in
# report order. Digits are data, never translated, so they live here rather
# than in i18n -- and HTML and PDF read them from here, so the two artefacts
# can never number the same report differently.
SECTION_VERDICT = "01"
SECTION_CHART = "02"
SECTION_TABLE = "03"
SECTION_ASSUMPTIONS = "04"

# Editorial palette shared with the HTML template (assets/report_template.html).
GAP_ROW_BG = HexColor("#fdf4f4")
UP_FG = HexColor("#127a3e")
DOWN_FG = HexColor("#b3261e")
MEDIUM_FG = HexColor("#8a5b00")
GAP_FG = HexColor("#a11111")
FLAT_FG = HexColor("#6b7785")

# grade -> badge ink. The badge is an outlined tag with no fill (so it
# survives a printer that drops backgrounds); its word plus the tile's dot
# meter separate the grades even in greyscale.
GRADE_INK: dict[str, Color] = {
    "high": UP_FG,
    "medium": MEDIUM_FG,
    "low": DOWN_FG,
    "gap": GAP_FG,
}

MAX_CHART_HEIGHT = 250.0

TABLE_COLUMNS = [
    ("Lang", 34),
    ("Article", 118),
    ("Views", 62),
    ("Share/M", 56),
    ("YoY abs", 58),
    ("YoY share", 62),
    ("R\u00b2", 38),
    ("Confidence", 104),
]
ALIGN_LEFT = {"Lang", "Article"}

# Which message carries each column's header. The *keys* stay English (they
# are also the row keys and the payload vocabulary); only the printed label is
# translated, so a localised table cannot drift from the data behind it.
COLUMN_MESSAGES = {
    "Lang": "report.col_lang",
    "Article": "report.col_article",
    "Views": "report.col_views",
    "Share/M": "report.col_share",
    "YoY abs": "report.col_yoy_abs",
    "YoY share": "report.col_yoy_share",
    "R\u00b2": "report.col_r2",
    "Confidence": "report.col_confidence",
}


class ReportOverflow(RuntimeError):
    """Raised when the report cannot be squeezed onto a single page."""


# --------------------------------------------------------------------------
# Fonts
# --------------------------------------------------------------------------
def _register_fonts() -> tuple[str, str]:
    """Register the Unicode font that ships inside the matplotlib wheel.

    Fonts are an explicitly optional resource: if matplotlib is not installed
    (``ImportError``), the files cannot be read (``OSError``), or reportlab
    rejects them (``PDFError``), the report falls back to Helvetica and the
    reason is printed on stderr. Anything else is a real bug and propagates.
    """
    reason: str | None = None
    try:
        import matplotlib

        # get_data_path() already points at .../matplotlib/mpl-data
        base = Path(matplotlib.get_data_path()) / "fonts" / "ttf"
        regular = base / "DejaVuSans.ttf"
        bold = base / "DejaVuSans-Bold.ttf"
        if regular.is_file() and bold.is_file():
            pdfmetrics.registerFont(TTFont("WIA-Sans", str(regular)))
            pdfmetrics.registerFont(TTFont("WIA-Sans-Bold", str(bold)))
            return "WIA-Sans", "WIA-Sans-Bold"
        reason = f"font files not found under {base}"
    except (ImportError, OSError, PDFError) as exc:  # pragma: no cover - host-dependent
        reason = f"{type(exc).__name__}: {exc}"
    print(
        f"warning: DejaVu fonts unavailable ({reason}); "
        "falling back to Helvetica, which cannot render every Latin-ext "
        "character (e.g. ř, š, ů)",
        file=sys.stderr,
    )
    return "Helvetica", "Helvetica-Bold"


FONT, FONT_BOLD = _register_fonts()


# --------------------------------------------------------------------------
# Text helpers
# --------------------------------------------------------------------------
def wrap(text: str, font: str, size: float, max_width: float) -> list[str]:
    """Word-wrap ``text`` to ``max_width`` points, measured with ``font``.

    Args:
        text: Input; each ``\\n``-separated paragraph wraps independently.
        font: Registered font name used to measure glyph widths.
        size: Font size in points.
        max_width: Available width in points.

    Returns:
        The lines to draw, in order; empty strings represent blank lines.
    """
    lines: list[str] = []
    for paragraph in str(text).split("\n"):
        words = paragraph.split()
        if not words:
            lines.append("")
            continue
        current = words[0]
        for word in words[1:]:
            trial = f"{current} {word}"
            if pdfmetrics.stringWidth(trial, font, size) <= max_width:
                current = trial
            else:
                lines.append(current)
                current = word
        lines.append(current)
    return lines


def fmt_pct(value: float | None, translator: i18n.Translator | None = None) -> str:
    """Format a percentage with an explicit sign, or ``"n/a"`` when undefined."""
    tr = translator or i18n.english()
    return tr.t("report.na") if value is None else f"{value:+.1f}%"


def fit(value: str, font: str, size: float, max_width: float) -> str:
    """Shorten ``value`` with an ellipsis until it measures within the width.

    Table columns have a fixed layout, so a header translated into a longer
    word would otherwise collide with its neighbour. English headers already
    fit, so this changes nothing for them.

    Args:
        value: Text to draw.
        font: Registered font name used to measure glyph widths.
        size: Font size in points.
        max_width: Available width in points.

    Returns:
        ``value`` unchanged when it fits, else a truncated form ending in an
        ellipsis (empty when even that does not fit).
    """
    if pdfmetrics.stringWidth(value, font, size) <= max_width:
        return value
    trimmed = value
    while trimmed and (
        pdfmetrics.stringWidth(trimmed + "\u2026", font, size) > max_width
    ):
        trimmed = trimmed[:-1]
    return f"{trimmed}\u2026" if trimmed else ""


# --------------------------------------------------------------------------
# Shared presentation (HTML and PDF read from the same structures)
# --------------------------------------------------------------------------
def headline_for(analysis: AnalysisPayload, translator: i18n.Translator) -> str:
    """The verdict sentence, in the report language.

    English reads the canonical ``headline`` string (so a hand-edited payload
    wins), every other language renders ``headline_i18n`` -- the message ref
    the English string was itself rendered from. A payload that predates the
    refs falls back to English *and* is counted as untranslated, because a
    report that silently switches language halfway is worse than a note.

    Args:
        analysis: The ``analysis.json`` payload.
        translator: Target language.

    Returns:
        The sentence to print.
    """
    if translator.is_english:
        return str(analysis["headline"])
    ref = analysis.get("headline_i18n")
    if ref:
        return translator.render(ref)
    translator.mark_untranslated("headline_i18n")
    return str(analysis["headline"])


def subtitle_for(
    analysis: AnalysisPayload, translator: i18n.Translator | None = None
) -> str:
    """Build the metadata line under the title (languages, window, access)."""
    tr = translator or i18n.english()
    params = analysis["parameters"]
    window = analysis["window"]
    return tr.t(
        "report.subtitle",
        langs=", ".join(analysis["metrics"]) or tr.t("report.no_data"),
        since=window["since"],
        until=window["until"],
        months=window.get("months", "?"),
        access=params["access"],
        agent=params["agent"],
        generated=analysis["generated_at"],
    )


def footer_for(
    analysis: AnalysisPayload, translator: i18n.Translator | None = None
) -> str:
    """Build the attribution line: data source, methods, concept id."""
    tr = translator or i18n.english()
    return tr.t(
        "report.footer",
        qid=str(analysis.get("qid") or tr.t("report.unresolved")),
    )


def table_rows_for(
    analysis: AnalysisPayload, translator: i18n.Translator | None = None
) -> list[dict[str, str]]:
    """Build the comparison table: one row per measured language, then gaps.

    Both renderers consume these rows, so the HTML and PDF tables can never
    disagree. Each row carries a ``"gap"`` marker (empty or ``"gap"``) that
    the renderers use to colour and align coverage-gap rows.

    Args:
        analysis: The ``analysis.json`` payload.
        translator: Target language; the confidence *labels* are translated,
            the grade itself (``high``/``medium``/``low``) stays the value
            stored in ``analysis.json``.

    Returns:
        Row dicts keyed by column name, plus the ``"gap"`` marker and the
        presentation hints: ``"grade"`` (confidence pill class), ``"bar"``
        (Share/M data-bar fraction, empty when there is nothing to scale)
        and ``"colour"`` (the language's series colour, shared with the
        chart). The PDF reads only what it draws; the HTML uses the hints.
    """
    tr = translator or i18n.english()
    # Data bars (HTML only): each Share/M cell's bar is this row's share as a
    # fraction of the largest measured share, so the bars scale against real
    # data. Purely presentational -- the number beside it stays the truth,
    # and the bar can vanish entirely when backgrounds are not printed.
    peak_share = max(
        (float(metric["share_ppm"]) for metric in analysis["metrics"].values()),
        default=0.0,
    )
    rows: list[dict[str, str]] = []
    for language, metric in analysis["metrics"].items():
        yoy = metric["yoy"]
        rows.append(
            {
                "gap": "",
                "grade": metric["confidence"],
                "bar": (
                    f"{metric['share_ppm'] / peak_share:.3f}" if peak_share > 0 else ""
                ),
                "colour": _colour_for(analysis, language),
                "Lang": language,
                "Article": metric["article_title"],
                "Views": f"{metric['article_total']:,}",
                "Share/M": f"{metric['share_ppm']:.2f}",
                "YoY abs": fmt_pct(yoy.get("article_pct"), tr),
                "YoY share": fmt_pct(yoy.get("share_pct"), tr),
                "R\u00b2": f"{metric['trend']['share']['r2']:.2f}",
                "Confidence": tr.t(f"confidence.{metric['confidence']}").upper(),
            }
        )
    for language in analysis.get("gaps", []):
        rows.append(
            {
                "gap": "gap",
                "grade": "gap",
                "bar": "",
                "colour": "",
                "Lang": language,
                "Article": tr.t("report.gap_article"),
                "Views": "\u2014",
                "Share/M": "\u2014",
                "YoY abs": "\u2014",
                "YoY share": "\u2014",
                "R\u00b2": "\u2014",
                "Confidence": tr.t("report.gap_confidence"),
            }
        )
    return rows


def gaps_note(
    analysis: AnalysisPayload, translator: i18n.Translator | None = None
) -> str:
    """Inline HTML noting languages with no article (empty string if none)."""
    tr = translator or i18n.english()
    gaps = analysis.get("gaps", [])
    if not gaps:
        return ""
    note = tr.t(
        "report.gaps_note",
        langs=tr.t("join.comma").join(gaps),
    )
    return f' <span class="gap">{html.escape(note)}</span>'


# --------------------------------------------------------------------------
# Editorial components (shared vocabulary: HTML builds them, PDF echoes them)
# --------------------------------------------------------------------------
def trend_class(value: float | None) -> str:
    """Map a signed percentage to the ``up``/``down``/``flat`` style class.

    Args:
        value: A percentage, or ``None`` when the metric is unavailable.

    Returns:
        ``"up"`` for a positive change, ``"down"`` for a negative one, and
        ``"flat"`` for zero *or* ``None`` -- "no data" must not read as a
        movement in either direction.
    """
    if value is None or value == 0:
        return "flat"
    return "up" if value > 0 else "down"


def trend_arrow(kind: str) -> str:
    """The glyph for a ``trend_class`` result ("" for ``flat``)."""
    return {"up": "\u25b2", "down": "\u25bc"}.get(kind, "")


def confidence_pill_html(grade: str, label: str) -> str:
    """A confidence badge: ``<span class="pill pill-high">HIGH</span>``.

    Args:
        grade: ``high``/``medium``/``low`` (or ``gap`` for a coverage gap) --
            it selects the CSS class, never the wording.
        label: Already-translated, already-cased text to show inside.

    Returns:
        The escaped badge markup; an unknown grade degrades to ``pill-gap``
        styling rather than dropping the badge.
    """
    css = grade if grade in GRADE_INK else "gap"
    return f'<span class="pill pill-{css}">{html.escape(label)}</span>'


def confidence_gauge(grade: str) -> str:
    """A three-dot meter for a confidence grade: ``<span class="gauge">●●○</span>``.

    Filled dots count the grade (high 3, medium 2, low 1; a coverage gap or
    an unknown grade reads as none). The meter is decorative (``aria-hidden``)
    and deliberately *shape*-based, so the grades stay apart in greyscale
    print even where the badge's colour does not survive.

    Args:
        grade: ``high``/``medium``/``low`` (or ``gap``/unknown -- no dots).

    Returns:
        The meter markup with exactly three dots, escaped as literal glyphs.
    """
    filled = {"high": 3, "medium": 2, "low": 1}.get(grade, 0)
    dots = "\u25cf" * filled + "\u25cb" * (3 - filled)
    return f'<span class="gauge" aria-hidden="true">{dots}</span>'


def hero_html(
    analysis: AnalysisPayload, translator: i18n.Translator | None = None
) -> str:
    """The hero number: article views combined over every measured edition.

    One figure for the masthead area -- the verdict says which way interest
    is moving, this says how many readers it reaches in total. It is the
    plain sum of the same ``article_total`` values printed per language in
    the table, so it introduces no data of its own; when nothing is measured
    there is *no* hero rather than a fabricated 0.

    Args:
        analysis: The ``analysis.json`` payload.
        translator: Target language; English when omitted.

    Returns:
        The interior HTML for the verdict's hero slot ("" with no metrics).
    """
    tr = translator or i18n.english()
    metrics = analysis["metrics"]
    if not metrics:
        return ""
    total = sum(int(metric["article_total"]) for metric in metrics.values())
    langs = html.escape(tr.t("join.comma").join(metrics))
    return (
        '<div class="hero-rule"></div>'
        f'<div class="hero-num">{total:,}</div>'
        f'<div class="hero-label">{html.escape(tr.t("report.hero_label"))}</div>'
        f'<div class="hero-sub">{langs}</div>'
    )


def _colour_for(analysis: AnalysisPayload, language: str) -> str:
    """The series colour for ``language`` (same order as the chart lines)."""
    measured = [
        lang
        for lang, m in analysis["metrics"].items()
        if m.get("series") and m["series"]["labels"]
    ]
    try:
        index = measured.index(language)
    except ValueError:
        return common.SERIES_COLOURS[0]
    return common.SERIES_COLOURS[index % len(common.SERIES_COLOURS)]


#: Sparkline geometry shared by :func:`sparkline_svg` and
#: :func:`sparkline_end_fraction`, so the drawn end point and the label that
#: sits beside it can never disagree about where the line lands.
SPARK_WIDTH = 160
SPARK_HEIGHT = 30
SPARK_PAD = 2.0


def spark_points(
    values: list[float],
    width: int = SPARK_WIDTH,
    height: int = SPARK_HEIGHT,
) -> list[tuple[float, float]]:
    """Min-max scaled ``values`` as sparkline coordinates (chronological).

    Args:
        values: The monthly series (share per million), at least two points.
        width: Viewport width in user units.
        height: Viewport height in user units.

    Returns:
        ``(x, y)`` pairs inset by ``SPARK_PAD``; a flat series collapses to a
        straight horizontal line rather than dividing by zero.
    """
    lo, hi = min(values), max(values)
    span = (hi - lo) or 1.0
    step = (width - 2 * SPARK_PAD) / (len(values) - 1)
    return [
        (
            SPARK_PAD + index * step,
            height - SPARK_PAD - ((value - lo) / span) * (height - 2 * SPARK_PAD),
        )
        for index, value in enumerate(values)
    ]


def sparkline_end_fraction(values: list[float]) -> float:
    """Where the sparkline's last point sits, as a fraction of its height.

    Used to place the end-point label *level with the end of the line*
    (the CSS translates the label by ``(fraction - 0.5) * SPARK_HEIGHT``).
    The range is clamped so a point at the very edge cannot push the label
    out of the tile -- at that distance the alignment is decorative anyway.

    Args:
        values: The monthly series, at least two points.

    Returns:
        ``0.0`` (top) .. ``1.0`` (bottom), clamped to ``0.18`` .. ``0.82``;
        ``0.5`` when there is nothing to measure.
    """
    if len(values) < 2:
        return 0.5
    fraction = spark_points(values)[-1][1] / SPARK_HEIGHT
    return min(0.82, max(0.18, fraction))


def sparkline_svg(
    values: list[float],
    colour: str,
    alt: str,
    width: int = SPARK_WIDTH,
    height: int = SPARK_HEIGHT,
) -> str:
    """A tiny inline SVG line chart of ``values`` (self-contained, no scripts).

    Swiss styling: the line *is* the chart -- no area fill -- with the last
    month marked by a dot so the label next to the graphic has an anchor.

    Args:
        values: The monthly series to plot (share per million).
        colour: Hex stroke colour; also the end-point dot's fill.
        alt: Accessible description (translated by the caller).
        width: Viewport width in user units.
        height: Viewport height in user units.

    Returns:
        An ``<svg>`` string, or ``""`` when there is nothing honest to draw
        (fewer than two points cannot show a trend).
    """
    if len(values) < 2:
        return ""
    points = spark_points(values, width, height)
    line = " ".join(f"{x:.1f},{y:.1f}" for x, y in points)
    end_x, end_y = points[-1]
    label = html.escape(alt)
    return (
        f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="{label}" '
        f'preserveAspectRatio="none">'
        f'<polyline points="{line}" fill="none" stroke="{colour}" '
        f'stroke-width="1.6" stroke-linejoin="round" stroke-linecap="round"/>'
        f'<circle cx="{end_x:.1f}" cy="{end_y:.1f}" r="2.4" fill="{colour}"/>'
        f"</svg>"
    )


def kpi_tiles_for(
    analysis: AnalysisPayload, translator: i18n.Translator | None = None
) -> str:
    """The KPI tile grid: one card per measured language, then gap cards.

    Each tile answers "how big, which way, how much do we trust it" without
    reading the table: a big YoY-share number with an arrow, the two
    supporting counts, a confidence pill plus its three-dot meter, and a
    sparkline of the real monthly share series labelled with the latest
    month's value. Coverage gaps get their own dashed card, so a missing
    article stays visible instead of vanishing from the summary.

    Args:
        analysis: The ``analysis.json`` payload.
        translator: Target language; English when omitted.

    Returns:
        HTML for a ``<section class="kpis">`` block ("" with no metrics *and*
        no gaps -- nothing to summarise).
    """
    tr = translator or i18n.english()
    tiles: list[str] = []
    for language, metric in analysis["metrics"].items():
        yoy = metric["yoy"]
        share_pct = yoy.get("share_pct")
        kind = trend_class(share_pct)
        series = metric.get("series")
        values = list(series["share_ppm"]) if series else []
        sparkline = sparkline_svg(
            values,
            _colour_for(analysis, language),
            tr.t("report.sparkline_alt", lang=language),
        )
        spark_row = ""
        if len(values) >= 2:
            # The label names the *latest* month (the stat above shows the
            # window's average share) and is translated vertically to sit
            # level with the dot that marks that month on the line.
            offset = sparkline_end_fraction(values) - 0.5
            spark_row = (
                f'<div class="spark-row">{sparkline}'
                f'<span class="spark-end" style="--dy:{offset:.3f}">'
                f"{values[-1]:.2f}</span></div>"
            )
        pill = confidence_pill_html(
            metric["confidence"],
            tr.t(f"confidence.{metric['confidence']}").upper(),
        )
        tiles.append(
            '<article class="tile" style="--series:{}">'
            '<div class="tile-head">'
            '<span class="tile-lang">{}</span>'
            '<span class="tile-badges">{}{}</span></div>'
            '<div class="tile-title">{}</div>'
            '<div class="tile-hero {}">'
            '<span class="arrow" aria-hidden="true">{}</span>{}</div>'
            '<div class="tile-hero-label">{}</div>'
            '<div class="tile-stats">'
            '<div class="tile-stat"><b>{}</b><span>{}</span></div>'
            '<div class="tile-stat"><b>{}</b><span>{}</span></div>'
            "</div>{}</article>".format(
                _colour_for(analysis, language),
                html.escape(language),
                pill,
                confidence_gauge(metric["confidence"]),
                html.escape(metric["article_title"]),
                kind,
                trend_arrow(kind),
                html.escape(fmt_pct(share_pct, tr)),
                html.escape(tr.t("report.kpi_yoy")),
                f"{metric['article_total']:,}",
                html.escape(tr.t("report.kpi_views")),
                f"{metric['share_ppm']:.2f}",
                html.escape(tr.t("report.kpi_share")),
                spark_row,
            )
        )
    for language in analysis.get("gaps", []):
        gap_pill = confidence_pill_html("gap", tr.t("report.gap_confidence"))
        tiles.append(
            '<article class="tile gap-tile">'
            '<div class="tile-head">'
            f'<span class="tile-lang">{html.escape(language)}</span>'
            f'<span class="tile-badges">{gap_pill}{confidence_gauge("gap")}'
            "</span></div>"
            f'<div class="tile-title">{html.escape(tr.t("report.gap_article"))}</div>'
            '<div class="tile-hero flat"><span class="arrow" aria-hidden="true"></span>'
            f'{html.escape(tr.t("report.na"))}</div>'
            f'<div class="tile-hero-label">{html.escape(tr.t("report.kpi_yoy"))}</div>'
            "</article>"
        )
    if not tiles:
        return ""
    return '<section class="kpis">' + "".join(tiles) + "</section>"


# --------------------------------------------------------------------------
# HTML
# --------------------------------------------------------------------------
def caveat_items(
    analysis: AnalysisPayload, translator: i18n.Translator | None = None
) -> list[str]:
    """Everything the reader must weigh: warnings, then limitations, then assumptions.

    Runtime warnings used to reach only ``analysis.json``, so a reader of the
    one-page PDF could not tell that e.g. an unalignable window had capped the
    confidence grade. The report is the shareable artifact; it must carry them.
    Exact duplicates are dropped so repeated wording cannot spend one-page space.

    Args:
        analysis: The ``analysis.json`` payload.
        translator: Target language. English reads the canonical strings from
            the payload; other languages render the ``*_i18n`` refs they were
            built from (the warning *prefix* is translated, but a runtime
            warning's wording comes from the fetch stage and stays English).

    Returns:
        The caveats, in report order, de-duplicated.
    """
    tr = translator or i18n.english()
    if tr.is_english:
        limitations = list(analysis.get("limitations") or [])
        assumptions = list(analysis.get("assumptions") or [])
    else:
        limitations = _render_caveats(
            analysis.get("limitations_i18n"),
            "limitations_i18n",
            analysis.get("limitations"),
            tr,
        )
        assumptions = _render_caveats(
            analysis.get("assumptions_i18n"),
            "assumptions_i18n",
            analysis.get("assumptions"),
            tr,
        )
    ordered = (
        [tr.t("caveat.warning", item=item) for item in analysis.get("warnings", [])]
        + limitations
        + assumptions
    )
    seen: set[str] = set()
    unique: list[str] = []
    for item in ordered:
        if item not in seen:
            seen.add(item)
            unique.append(item)
    return unique


def _render_caveats(
    refs: list[MessageRef] | None,
    ref_key: str,
    english_texts: list[str] | None,
    translator: i18n.Translator,
) -> list[str]:
    """Render caveat refs in the report language, or admit they are English.

    A payload written before the ``*_i18n`` structures existed has only the
    English strings: rendering them and *recording* them means the report's
    note still tells the truth about what is untranslated.
    """
    if refs:
        return [translator.render(ref) for ref in refs]
    translator.mark_untranslated(ref_key)
    return list(english_texts or [])


def _bar_tint(colour: str) -> str:
    """A series colour turned into the translucent tint of a Share/M data bar.

    The alpha lives here (rather than in the hex) so the CSS gradient can
    run from the tint to ``transparent`` at the bar's end using one colour.

    Args:
        colour: Hex colour in ``#rrggbb`` form (from ``common.SERIES_COLOURS``).

    Returns:
        ``rgba(...)`` text safe to drop into an inline style attribute.
    """
    red, green, blue = (int(colour[index : index + 2], 16) for index in (1, 3, 5))
    return f"rgba({red}, {green}, {blue}, 0.18)"


def _table_cell(name: str, row: dict[str, str]) -> str:
    """One ``<td>``; styled by role, with gap markers preserved.

    YoY cells get the trend class (green/red/grey) so direction reads at a
    glance, the Confidence cell becomes a pill, and Share/M carries a
    data-bar tint scaled by ``row["bar"]`` -- all purely presentational:
    the text inside is exactly what :func:`table_rows_for` produced.

    The direction is read from the *sign of the rendered text* (``+``/``-``)
    rather than the raw number: the cell is what the reader sees, so what is
    coloured can never disagree with what is printed.
    """
    value = html.escape(row[name])
    if row["gap"] and name == "Article":
        return f'<td><span class="gap">{value}</span></td>'
    if row["gap"]:
        return f'<td class="flat">{value}</td>'
    if name == "Share/M" and row.get("bar"):
        # The bar is a translucent gradient *behind* the number, scaled by
        # the caller. It is decoration: drop the backgrounds and the cell
        # still reads as the plain value it was always going to show.
        return (
            f'<td class="bar-cell" style="--w:{row["bar"]};'
            f'--bar:{_bar_tint(row["colour"])}">{value}</td>'
        )
    if name in {"YoY abs", "YoY share"}:
        if value.startswith("+"):
            kind = "up"
        elif value.startswith("-"):
            kind = "down"
        else:
            kind = "flat"
        return f'<td class="{kind}">{value}</td>'
    if name == "Confidence":
        return f"<td>{confidence_pill_html(row['grade'], value)}</td>"
    return f"<td>{value}</td>"


def _table_row(row: dict[str, str]) -> str:
    """One ``<tr>``; a coverage-gap row also carries the ``gap-row`` class.

    The class is what paints the row red/italic in the template's CSS, so a
    missing article stays visible as a *row*, not just as red cell text.
    """
    cls = ' class="gap-row"' if row["gap"] else ""
    cells = "".join(_table_cell(name, row) for name, _ in TABLE_COLUMNS)
    return f"<tr{cls}>{cells}</tr>"


def render_html(
    analysis: AnalysisPayload,
    chart_png: Path | None,
    out_path: Path,
    translator: i18n.Translator | None = None,
) -> None:
    """Write the self-contained HTML report (chart embedded as base64).

    Args:
        analysis: The ``analysis.json`` payload.
        chart_png: Chart to embed; a missing/absent file degrades to a
            placeholder note rather than breaking the report.
        out_path: Output file; parent directories are created.
        translator: Target language; English when omitted.

    Raises:
        OSError: If the template or output cannot be read/written.
        KeyError: If a placeholder's section is missing from the payload.
    """
    tr = translator or i18n.english()
    template = string.Template(TEMPLATE_PATH.read_text(encoding="utf-8"))

    if chart_png and chart_png.is_file():
        encoded = base64.b64encode(chart_png.read_bytes()).decode("ascii")
        figure_html = (
            f'<figure><img alt="{html.escape(tr.t("report.chart_alt"))}" '
            f'src="data:image/png;base64,{encoded}"/>'
            f"<figcaption>{html.escape(tr.t('report.chart_caption'))}"
            "</figcaption></figure>"
        )
    else:
        figure_html = (
            f'<figure class="gap">{html.escape(tr.t("report.chart_missing"))}'
            "</figure>"
        )
    # Every major section opens with a rule and its number (Swiss grid).
    chart_block = (
        f'<div class="sec"><span class="sec-num">{SECTION_CHART}</span>'
        f"{figure_html}</div>"
    )

    header_cells = "".join(
        f"<th>{html.escape(tr.t(COLUMN_MESSAGES[name]))}</th>"
        for name, _ in TABLE_COLUMNS
    )
    body_rows = [
        _table_row(row) for row in table_rows_for(analysis, tr)
    ]

    table_block = (
        f'<div class="sec"><span class="sec-num">{SECTION_TABLE}</span>'
        "<table><thead><tr>"
        + header_cells
        + "</tr></thead><tbody>"
        + "".join(body_rows)
        + "</tbody></table></div>"
    )

    bullets = "".join(
        f"<li>{html.escape(item)}</li>" for item in caveat_items(analysis, tr)
    )

    # Everything the reader sees is rendered first: the note counts the
    # strings that fell back to English, so it is only accurate afterwards.
    title = tr.t("report.title", topic=str(analysis["topic"]))
    subtitle = subtitle_for(analysis, tr)
    kicker = tr.t("report.kicker")
    # The tag carries its section number: digits are never translated.
    verdict_tag = f"{SECTION_VERDICT} \u00b7 {tr.t('report.verdict_tag')}"
    heading = tr.t("report.assumptions_heading")
    heading_block = (
        f'<div class="sec"><span class="sec-num">{SECTION_ASSUMPTIONS}</span>'
        f"<h2>{html.escape(heading)}</h2></div>"
    )
    footer = footer_for(analysis, tr)
    headline = headline_for(analysis, tr)
    gap_note = gaps_note(analysis, tr)
    kpi_block = kpi_tiles_for(analysis, tr)
    hero_block = hero_html(analysis, tr)
    note = tr.note()
    note_block = (
        f'<p class="gap"><strong>Warning</strong> ({html.escape(tr.lang)}): '
        f"{html.escape(note)}</p>"
        if note
        else ""
    )

    rendered = template.substitute(
        html_lang=html.escape(tr.display_lang),
        title=html.escape(title),
        kicker=html.escape(kicker),
        verdict_tag=html.escape(verdict_tag),
        h1=html.escape(title),
        subtitle=html.escape(subtitle),
        headline=html.escape(headline) + gap_note,
        hero_block=hero_block,
        kpi_block=kpi_block,
        chart_block=chart_block,
        table_block=table_block,
        heading_block=heading_block,
        limitations=bullets,
        note_block=note_block,
        footer=html.escape(footer),
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(rendered, encoding="utf-8")


# --------------------------------------------------------------------------
# PDF -- strictly one page
# --------------------------------------------------------------------------
class _Sheet:
    """Vertical layout cursor that refuses to draw past the bottom margin."""

    def __init__(self, writer: pdf_canvas.Canvas) -> None:
        """Place the cursor at the top margin of ``writer``."""
        self.writer = writer
        self.y = PAGE_H - MARGIN

    def need(self, height: float, what: str) -> None:
        """Reserve ``height`` points for the block named ``what``.

        Raises:
            ReportOverflow: When the reservation would cross the bottom
                margin; the message names the block and the shortfall.
        """
        if self.y - height < BOTTOM:
            shortfall = BOTTOM - (self.y - height)
            raise ReportOverflow(
                f"report does not fit on one page: '{what}' needs {height:.0f}pt "
                f"but only {max(0.0, self.y - BOTTOM):.0f}pt remained "
                f"(short by {shortfall:.0f}pt). Shorten that block, drop a "
                "language, or widen the window's summary text."
            )

    def text(
        self,
        value: str,
        font: str,
        size: float,
        leading: float,
        *,
        max_width: float = CONTENT_W,
        color: Color = INK,
        x: float = MARGIN,
        what: str = "text",
        indent: float = 0.0,
    ) -> None:
        """Wrap and draw one block, refusing first if it would overflow.

        Args:
            value: Text to draw (wrapped to ``max_width - indent``).
            font: Registered font name (``FONT``/``FONT_BOLD``).
            size: Font size in points.
            leading: Baseline-to-baseline distance in points.
            max_width: Block width measured from ``x`` (keyword-only).
            color: Reportlab colour for the text (keyword-only).
            x: Left edge in points (keyword-only).
            what: Block name used in the overflow message (keyword-only).
            indent: Extra left inset for the wrapped lines (keyword-only).
        """
        lines = wrap(value, font, size, max_width - indent)
        self.need(leading * len(lines) + 2, what)
        self.writer.setFont(font, size)
        self.writer.setFillColor(color)
        for line in lines:
            self.y -= leading
            self.writer.drawString(x + indent, self.y, line)
        self.y -= 2

    def gap(self, height: float) -> None:
        """Move the cursor down without drawing (spacing only)."""
        self.y -= height


def _cell_colour(name: str, value: str) -> Color:
    """Colour for a numeric table cell: trend direction, else neutral ink.

    Args:
        name: Column name (only the YoY columns are trend-coloured).
        value: The rendered cell text; its sign is the direction the reader
            sees, so the colour can never contradict the number.

    Returns:
        Reportlab colour to draw the value in.
    """
    if name in {"YoY abs", "YoY share"}:
        if value.startswith("+"):
            return UP_FG
        if value.startswith("-"):
            return DOWN_FG
    return INK


def _draw_pill(
    writer: pdf_canvas.Canvas,
    grade: str,
    label: str,
    x: float,
    width: float,
    row_y: float,
    row_height: float,
) -> None:
    """Draw the confidence badge as an outlined, square-cornered tag.

    No fill on purpose: this is the print-safety rule and the Swiss idiom at
    once -- a badge that survives a printer dropping backgrounds, where the
    word inside (and the tile's dot meter) separates the grades even in
    greyscale.

    Args:
        writer: Canvas to draw on.
        grade: ``high``/``medium``/``low``/``gap`` -- selects the ink;
            an unknown grade degrades to the ``gap`` palette.
        label: Already-translated badge text.
        x: Column left edge.
        width: Column width.
        row_y: Row bottom (the cursor position for this row).
        row_height: Row height, so the tag centres vertically in it.
    """
    ink = GRADE_INK.get(grade, GRADE_INK["gap"])
    size = 7.0
    pill_h = 9.5
    max_w = width - 6
    # A translation wider than its column is shortened rather than spilling
    # over the neighbour (same rule the header row follows).
    label = fit(label, FONT_BOLD, size, max_w - 10)
    text_w = pdfmetrics.stringWidth(label, FONT_BOLD, size)
    pill_w = min(text_w + 10, max_w)
    pill_x = x + width - 4 - pill_w
    pill_y = row_y + (row_height - pill_h) / 2
    writer.setStrokeColor(ink)
    writer.setLineWidth(0.7)
    writer.rect(pill_x, pill_y, pill_w, pill_h, stroke=1, fill=0)
    writer.setFillColor(ink)
    writer.setFont(FONT_BOLD, size)
    writer.drawCentredString(pill_x + pill_w / 2, pill_y + 3.0, label)


#: Vertical space the PDF's hero figure occupies below the verdict box top
#: (rule, number, label, languages -- see :func:`_draw_hero_pdf`).
HERO_H = 66.0


def _hero_pdf(
    analysis: AnalysisPayload, translator: i18n.Translator
) -> tuple[str, str, str, float, float] | None:
    """Measure the PDF's hero figure: ``(number, label, languages, size, width)``.

    The same combined total :func:`hero_html` prints in the HTML; ``width``
    is what the verdict text must wrap around. The number shrinks rather
    than squeeze the sentence when an unusually large total would claim too
    much of the line.

    Args:
        analysis: The ``analysis.json`` payload.
        translator: Target language (label and language list).

    Returns:
        The measured block, or ``None`` when nothing is measurable (there
        is no honest total to show, so the verdict spans the full width).
    """
    metrics = analysis["metrics"]
    if not metrics:
        return None
    total = sum(int(metric["article_total"]) for metric in metrics.values())
    number = f"{total:,}"
    label = translator.t("report.hero_label").upper()
    languages = translator.t("join.comma").join(metrics)
    max_number_w = CONTENT_W * 0.42
    size = 26.0
    while size > 14 and (
        pdfmetrics.stringWidth(number, FONT_BOLD, size) > max_number_w
    ):
        size -= 1.0
    width = max(
        pdfmetrics.stringWidth(number, FONT_BOLD, size),
        pdfmetrics.stringWidth(label, FONT_BOLD, 7.0),
        pdfmetrics.stringWidth(languages, FONT, 7.5),
    ) + 6
    return number, label, languages, size, width


def _draw_hero_pdf(
    writer: pdf_canvas.Canvas,
    hero: tuple[str, str, str, float, float],
    box_top: float,
) -> None:
    """Draw the hero figure flush right inside the verdict box.

    A red rule over a large black numeral with its label underneath: the
    accent stays a rule (never a fill), and the number is data, so it is
    drawn in ink rather than colour.

    Args:
        writer: Canvas to draw on.
        hero: The measured block from :func:`_hero_pdf`.
        box_top: Top edge of the verdict box to hang the figure from.
    """
    number, label, languages, size, _width = hero
    right = PAGE_W - MARGIN - 11
    cursor = box_top - 8
    writer.setFillColor(ACCENT)
    writer.rect(right - 44, cursor, 44, 2.5, stroke=0, fill=1)
    cursor -= 4 + size
    writer.setFillColor(INK)
    writer.setFont(FONT_BOLD, size)
    writer.drawRightString(right, cursor, number)
    cursor -= 11
    writer.setFont(FONT_BOLD, 7.0)
    writer.drawRightString(right, cursor, label)
    cursor -= 10
    writer.setFont(FONT, 7.5)
    writer.setFillColor(MUTED)
    writer.drawRightString(right, cursor, languages)


def _draw_spaced(
    writer: pdf_canvas.Canvas,
    text: str,
    x: float,
    y: float,
    font: str,
    size: float,
    spacing: float,
) -> None:
    """Draw ``text`` at ``(x, y)`` with Swiss-style letterspacing.

    reportlab's canvas exposes no character-spacing state, so spaced caps
    are drawn glyph by glyph: each one stepped by its own measured width
    plus ``spacing``.

    Args:
        writer: Canvas to draw on (the font must already be set).
        text: The string to draw.
        x: Left edge of the first glyph.
        y: Baseline.
        font: Registered font name used to measure glyph widths.
        size: Font size in points.
        spacing: Extra points inserted between glyphs.
    """
    cursor = x
    for char in text:
        writer.drawString(cursor, y, char)
        cursor += pdfmetrics.stringWidth(char, font, size) + spacing


def render_pdf(
    analysis: AnalysisPayload,
    chart_png: Path | None,
    out_path: Path,
    translator: i18n.Translator | None = None,
) -> None:
    """Write the strictly-one-page PDF, or raise ``ReportOverflow``.

    The layout cursor (``_Sheet``) is checked before every block, so a report
    that cannot fit fails loudly instead of silently emitting a second page --
    no output file is written at all in that case.

    Args:
        analysis: The ``analysis.json`` payload.
        chart_png: Chart to draw scaled; ignored when missing/unreadable.
        out_path: Output file; the caller decides what to do on failure.
        translator: Target language; English when omitted.

    Raises:
        ReportOverflow: When any block would cross the bottom margin.
    """
    tr = translator or i18n.english()
    writer = pdf_canvas.Canvas(str(out_path), pagesize=letter)
    title = tr.t("report.title", topic=str(analysis["topic"]))
    writer.setTitle(title)
    writer.setAuthor("wikipedia-interest-analyzer")
    sheet = _Sheet(writer)

    # --- header: Swiss masthead ---------------------------------------------
    # Flush-left black type on white paper with one red rule at its foot, and
    # the kicker (the HTML's, at last) in letterspaced red above the title.
    # White by design: a printer that drops backgrounds still gets all of it,
    # because nothing here depends on a fill.
    subtitle = subtitle_for(analysis, tr)
    kicker = tr.t("report.kicker")
    title_lines = wrap(title, FONT_BOLD, 17, CONTENT_W)
    subtitle_lines = wrap(subtitle, FONT, 8, CONTENT_W)
    # The kicker is drawn with character spacing, which the wrapper cannot
    # know: reserve the space it adds (about 1.6pt per character).
    kicker_lines = wrap(kicker, FONT_BOLD, 7.5, CONTENT_W - 40)
    band_height = (
        8
        + 11 * len(kicker_lines)
        + 21 * len(title_lines)
        + 11 * len(subtitle_lines)
        + 8
    )
    sheet.need(band_height + 10, "header band")
    band_top = sheet.y + 6
    band_bottom = band_top - band_height
    cursor = band_top - 8
    writer.setFont(FONT_BOLD, 7.5)
    writer.setFillColor(ACCENT)
    for line in kicker_lines:
        cursor -= 11
        _draw_spaced(writer, line, MARGIN, cursor, FONT_BOLD, 7.5, 1.6)
    writer.setFont(FONT_BOLD, 17)
    for line in title_lines:
        cursor -= 21
        writer.setFillColor(INK)
        writer.drawString(MARGIN, cursor, line)
    writer.setFont(FONT, 8)
    for line in subtitle_lines:
        cursor -= 11
        writer.setFillColor(MUTED)
        writer.drawString(MARGIN, cursor, line)
    writer.setFillColor(ACCENT)
    writer.rect(MARGIN, band_bottom, CONTENT_W, 3, stroke=0, fill=1)
    sheet.y = band_top - band_height - 10

    # --- verdict: red bar, outlined tag, hero figure ------------------------
    # No panel fill: white paper, a red bar on the left and a black hairline
    # to close the block -- so it also survives a printer that drops
    # backgrounds. ``headline`` already ends with the coverage-gap sentence
    # when there are gaps (analyze.py appends it), so the PDF prints it
    # exactly once -- a second copy cost page space and read as a stutter.
    verdict = headline_for(analysis, tr)
    tag = f"{SECTION_VERDICT} \u00b7 {tr.t('report.verdict_tag').upper()}"
    hero = _hero_pdf(analysis, tr)
    hero_w = hero[4] if hero else 0.0
    verdict_lines = wrap(verdict, FONT, 9.5, CONTENT_W - 22 - hero_w)
    tag_width = pdfmetrics.stringWidth(tag, FONT_BOLD, 7.0) + 12
    text_height = 14 + 12.5 * len(verdict_lines) + 16
    box_height = max(text_height, HERO_H if hero else 0.0)
    sheet.need(box_height + 10, "verdict box")
    box_top = sheet.y
    writer.setFillColor(ACCENT)
    writer.rect(MARGIN, box_top - box_height, 3, box_height, stroke=0, fill=1)
    # Tag: outlined, square-cornered, carrying its section number.
    writer.setStrokeColor(ACCENT)
    writer.setLineWidth(0.7)
    writer.rect(MARGIN + 11, box_top - 12, tag_width, 11, stroke=1, fill=0)
    writer.setFillColor(ACCENT)
    writer.setFont(FONT_BOLD, 7.0)
    writer.drawCentredString(MARGIN + 11 + tag_width / 2, box_top - 9.5, tag)
    writer.setFillColor(INK)
    writer.setFont(FONT, 9.5)
    cursor = box_top - 22
    for line in verdict_lines:
        cursor -= 12.5
        writer.drawString(MARGIN + 11, cursor, line)
    if hero:
        _draw_hero_pdf(writer, hero, box_top)
    writer.setStrokeColor(INK)
    writer.setLineWidth(0.75)
    writer.line(MARGIN, box_top - box_height, PAGE_W - MARGIN, box_top - box_height)
    sheet.y = box_top - box_height - 12

    # --- chart -------------------------------------------------------------
    if chart_png and chart_png.is_file():
        # Section number first: red, flush left, its own thin line.
        sheet.gap(4)
        sheet.need(11, "chart section number")
        sheet.y -= 9
        writer.setFont(FONT_BOLD, 8)
        writer.setFillColor(ACCENT)
        writer.drawString(MARGIN, sheet.y, SECTION_CHART)
        sheet.gap(4)
        image = ImageReader(str(chart_png))
        image_w, image_h = image.getSize()
        scale = min(CONTENT_W / image_w, MAX_CHART_HEIGHT / image_h)
        draw_w, draw_h = image_w * scale, image_h * scale
        sheet.need(draw_h + 20, "chart")
        sheet.y -= draw_h
        writer.drawImage(
            image,
            MARGIN + (CONTENT_W - draw_w) / 2,
            sheet.y,
            width=draw_w,
            height=draw_h,
        )
        sheet.gap(4)
        sheet.text(
            tr.t("report.figure_caption"),
            FONT,
            7.5,
            10,
            color=MUTED,
            what="chart caption",
        )
        sheet.gap(4)

    # --- table -------------------------------------------------------------
    rows = table_rows_for(analysis, tr)
    row_height = 12.5
    # 15pt of that is the section number's own line above the header rule.
    sheet.need(15 + 16 + row_height * len(rows) + 8, "comparison table")

    x_positions = []
    cursor_x = MARGIN
    for _, width in TABLE_COLUMNS:
        x_positions.append(cursor_x)
        cursor_x += width

    sheet.y -= 11
    writer.setFont(FONT_BOLD, 8)
    writer.setFillColor(ACCENT)
    writer.drawString(MARGIN, sheet.y, SECTION_TABLE)
    sheet.gap(4)

    header_top = sheet.y
    # Swiss table: rules, not a filled band -- the header row is black caps
    # between a heavy rule above and a hairline below.
    writer.setStrokeColor(INK)
    writer.setLineWidth(1.2)
    writer.line(MARGIN, header_top, PAGE_W - MARGIN, header_top)
    writer.setFont(FONT_BOLD, 7.8)
    writer.setFillColor(INK)
    for (name, width), x in zip(TABLE_COLUMNS, x_positions, strict=True):
        # A translated header can be longer than its fixed column: shorten it
        # rather than let it collide with the neighbour it shares a row with.
        value = fit(tr.t(COLUMN_MESSAGES[name]), FONT_BOLD, 7.8, width - 8)
        if name in ALIGN_LEFT:
            writer.drawString(x + 4, header_top - 11.5, value)
        else:
            right = x + width - 4
            writer.drawRightString(right, header_top - 11.5, value)
    sheet.y = header_top - 16
    writer.setLineWidth(0.5)
    writer.line(MARGIN, sheet.y, PAGE_W - MARGIN, sheet.y)

    for index, row in enumerate(rows):
        sheet.y -= row_height
        if row["gap"]:
            writer.setFillColor(GAP_ROW_BG)
            writer.rect(MARGIN, sheet.y, CONTENT_W, row_height, stroke=0, fill=1)
        for (name, width), x in zip(TABLE_COLUMNS, x_positions, strict=True):
            value = row[name]
            if name == "Confidence":
                _draw_pill(writer, row["grade"], value, x, width, sheet.y, row_height)
                continue
            if name in ALIGN_LEFT:
                writer.setFillColor(GAP_FG if row["gap"] else INK)
                writer.setFont(FONT, 7.8)
                writer.drawString(x + 4, sheet.y + 3.5, value)
            else:
                writer.setFont(FONT, 7.8)
                writer.setFillColor(
                    INK if row["gap"] else _cell_colour(name, value)
                )
                writer.drawRightString(x + width - 4, sheet.y + 3.5, value)
        # Hairlines between rows, the accent once under the last one.
        last = index == len(rows) - 1
        writer.setStrokeColor(ACCENT if last else RULE)
        writer.setLineWidth(1.0 if last else 0.5)
        writer.line(MARGIN, sheet.y, PAGE_W - MARGIN, sheet.y)
    sheet.gap(8)

    # --- assumptions & limitations ----------------------------------------
    heading = tr.t("report.assumptions_heading").upper()
    sheet.need(12, "limitations heading")
    # The section number sits in the left margin, level with the heading's
    # first line: no extra block, so no extra page space.
    first_baseline = sheet.y - 12
    writer.setFont(FONT_BOLD, 8)
    writer.setFillColor(ACCENT)
    writer.drawString(MARGIN, first_baseline, SECTION_ASSUMPTIONS)
    sheet.text(
        heading,
        FONT_BOLD,
        8.5,
        12,
        color=INK,
        x=MARGIN + 16,
        max_width=CONTENT_W - 16,
        what="limitations heading",
    )
    for item in caveat_items(analysis, tr):
        sheet.text(
            "\u2022  " + item,
            FONT,
            7.2,
            9.4,
            indent=10,
            max_width=CONTENT_W,
            what="limitations bullet",
        )

    # --- untranslated strings ---------------------------------------------
    # Rendered last, so its count is accurate; red so a half-English report
    # cannot be mistaken for a finished translation.
    note = tr.note()
    if note:
        sheet.text(
            "\u2022  " + note,
            FONT,
            7.2,
            9.4,
            indent=10,
            max_width=CONTENT_W,
            color=GAP_FG,
            what="untranslated note",
        )

    # --- footer ------------------------------------------------------------
    sheet.gap(6)
    writer.setStrokeColor(INK)
    writer.setLineWidth(0.5)
    writer.line(MARGIN, sheet.y, PAGE_W - MARGIN, sheet.y)
    sheet.gap(0)
    sheet.text(
        footer_for(analysis, tr), FONT, 6.8, 8.5, color=MUTED, what="footer"
    )

    writer.showPage()
    writer.save()


def build_parser() -> argparse.ArgumentParser:
    """Build the ``report.py`` command-line parser."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--analysis", default="analysis.json", help="input from analyze.py"
    )
    parser.add_argument("--chart", default="chart.png", help="chart PNG from chart.py")
    parser.add_argument("--html", default="report.html", help="HTML output path")
    parser.add_argument("--pdf", default="report.pdf", help="PDF output path")
    parser.add_argument(
        "--report-lang",
        default="",
        help="report language code (default: English), e.g. pl",
    )
    parser.add_argument(
        "--translations",
        default="",
        help="translations JSON (default: translations.<lang>.json)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Write the HTML report always, and the one-page PDF when it fits one page.

    Returns:
        ``0`` on success; ``1`` when the PDF overflowed (the HTML is still
        written and the partial PDF is deleted).
    """
    common.configure_console()
    args = build_parser().parse_args(argv)
    analysis = common.read_json(args.analysis)
    chart = Path(args.chart) if args.chart else None

    lang = i18n.normalize_lang(args.report_lang)
    fallback = Path(f"translations.{lang}.json")
    translations = Path(args.translations) if args.translations else fallback
    translator = i18n.translator_for(lang, translations)

    html_path = Path(args.html)
    render_html(analysis, chart, html_path, translator)
    print(f"wrote {html_path}")

    status = 0
    pdf_path = Path(args.pdf)
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        render_pdf(analysis, chart, pdf_path, translator)
    except ReportOverflow as exc:
        print(f"error: {exc}", file=sys.stderr)
        print(
            "The HTML report was still written; the PDF was not.",
            file=sys.stderr,
        )
        if pdf_path.exists():
            pdf_path.unlink()
        status = 1
    else:
        print(f"wrote {pdf_path} (1 page)")

    # Printed on both paths: an untranslated report must be flagged even when
    # the PDF failed for an unrelated reason.
    i18n.warn_untranslated(translator)
    return status


if __name__ == "__main__":
    raise SystemExit(main())
