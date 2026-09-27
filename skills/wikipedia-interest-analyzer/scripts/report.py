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
from collections.abc import Mapping
from pathlib import Path
from typing import TextIO

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

ACCENT = HexColor("#1f4e79")
ACCENT_DEEP = HexColor("#0f3358")
GOLD = HexColor("#e0a33e")
MUTED = HexColor("#666666")
RULE = HexColor("#d9d9d9")
VERDICT_BG = HexColor("#f3f7fb")
HEADER_BG = HexColor("#f0f0f0")
INK = HexColor("#1a1a1a")
WHITE = HexColor("#ffffff")
SUBTITLE_ON_DARK = HexColor("#cfe1f2")

# Editorial palette shared with the HTML template (assets/report_template.html).
ZEBRA = HexColor("#f6f9fc")
GAP_ROW_BG = HexColor("#fdf4f4")
UP_FG, UP_BG = HexColor("#127a3e"), HexColor("#e3f5ea")
DOWN_FG, DOWN_BG = HexColor("#b3261e"), HexColor("#fbe7e5")
MEDIUM_FG, MEDIUM_BG = HexColor("#8a5b00"), HexColor("#fdf3e0")
GAP_FG = HexColor("#a11111")
FLAT_FG = HexColor("#6b7785")

#: grade -> (pill background, pill text) for the confidence badges.
GRADE_PILLS: dict[str, tuple[Color, Color]] = {
    "high": (UP_BG, UP_FG),
    "medium": (MEDIUM_BG, MEDIUM_FG),
    "low": (DOWN_BG, DOWN_FG),
    "gap": (DOWN_BG, GAP_FG),
}

MAX_CHART_HEIGHT = 250.0

#: Printed column headers and their widths in points; the widths add up to
#: ``CONTENT_W`` exactly, so the grid fills the page edge to edge.
#:
#: The labels are **not translatable** -- a metric name is a code, and a
#: column this narrow cannot hold a translated phrase anyway. The decoding a
#: reader needs is printed under the table from the manifest's ``table_key``
#: block (:func:`table_key_items`), never from the translation catalogue.
TABLE_COLUMNS = [
    ("Lang", 34),
    ("Article", 118),
    ("Views", 62),
    ("Share/M", 60),
    ("YoY", 52),
    ("YoY share", 64),
    ("R\u00b2", 32),
    ("Confidence", 110),
]
ALIGN_LEFT = {"Lang", "Article"}

#: Columns whose header is an abbreviation: these must be defined under the
#: table, so each ships with an English fallback (:data:`TABLE_KEY_DEFAULTS`).
#: The plain columns ("Lang", "Article", "Views", "Confidence") read as their
#: own definition and need no entry -- an agent may still supply one.
TABLE_KEY_COLUMNS = ("Share/M", "YoY", "YoY share", "R\u00b2")

#: Heading of the key when the manifest does not carry its own.
TABLE_KEY_HEADING = "Table key"

#: Last-resort wording for a ``TABLE_KEY_COLUMNS`` entry the manifest omits.
#: These are deliberately plain English and deliberately *not* translatable:
#: a report that falls back here says so in its untranslated note.
TABLE_KEY_DEFAULTS: dict[str, str] = {
    "Share/M": (
        "Article views as a share of all reading in that edition, per million "
        "edition views."
    ),
    "YoY": (
        "Change in article pageviews, second half of the window against the "
        "first half."
    ),
    "YoY share": (
        "The same comparison applied to the normalised share, so overall wiki "
        "growth cancels out."
    ),
    "R\u00b2": (
        "How much of the month-to-month variation a straight line explains "
        "(0 to 1)."
    ),
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
        ``"grade"`` (confidence pill class) presentation hint.
    """
    tr = translator or i18n.english()
    rows: list[dict[str, str]] = []
    for language, metric in analysis["metrics"].items():
        yoy = metric["yoy"]
        rows.append(
            {
                "gap": "",
                "grade": metric["confidence"],
                "Lang": language,
                "Article": metric["article_title"],
                "Views": f"{metric['article_total']:,}",
                "Share/M": f"{metric['share_ppm']:.2f}",
                "YoY": fmt_pct(yoy.get("article_pct"), tr),
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
                "Lang": language,
                "Article": tr.t("report.gap_article"),
                "Views": "\u2014",
                "Share/M": "\u2014",
                "YoY": "\u2014",
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


def table_key_items(
    table_key: Mapping[str, str] | None = None,
    translator: i18n.Translator | None = None,
) -> list[tuple[str, str]]:
    """The ``(header, definition)`` pairs printed as the key under the table.

    Metric names are codes, so their decoding is *generated, not translated*:
    the wording comes from the study manifest's ``table_key`` block, written
    once per study by whoever runs the skill. A column in
    :data:`TABLE_KEY_COLUMNS` whose wording is missing falls back to
    :data:`TABLE_KEY_DEFAULTS` and is recorded on the translator, so a report
    that had to use the English fallback admits it instead of looking
    finished.

    Columns outside :data:`TABLE_KEY_COLUMNS` are printed only when the
    manifest supplies them -- they read as their own definition otherwise.

    Both renderers consume this list, so the HTML and PDF keys cannot
    disagree.

    Args:
        table_key: The manifest block (``column -> definition``), or None.
        translator: Target language; used only to record the English
            fallbacks, never to rewrite the supplied wording.

    Returns:
        One pair per column that has something to say, in table order.
    """
    tr = translator or i18n.english()
    supplied = dict(table_key or {})
    items: list[tuple[str, str]] = []
    for name, _ in TABLE_COLUMNS:
        definition = supplied.get(name)
        if not definition:
            if name not in TABLE_KEY_COLUMNS:
                continue
            definition = TABLE_KEY_DEFAULTS[name]
            tr.mark_untranslated("table_key")
        items.append((name, definition))
    return items


def table_key_heading(
    table_key: Mapping[str, str] | None = None,
    translator: i18n.Translator | None = None,
) -> str:
    """The line above the key: the manifest's ``heading``, else English.

    Args:
        table_key: The manifest block; ``"heading"`` is its optional title.
        translator: Target language, used only to record a fallback.

    Returns:
        The heading to print. An absent one is admitted as untranslated in a
        localised report rather than silently switching the reader's language.
    """
    tr = translator or i18n.english()
    heading = (table_key or {}).get("heading")
    if not heading:
        tr.mark_untranslated("table_key")
        return TABLE_KEY_HEADING
    return heading


def warn_unknown_table_key(
    table_key: Mapping[str, str] | None, stream: TextIO | None = None
) -> None:
    """Warn about ``table_key`` entries naming a column the table never prints.

    A definition the report silently drops is work thrown away, so the run
    says so instead. Valid non-column keys (``heading``) are never flagged.

    Args:
        table_key: The manifest block, possibly None.
        stream: Where the warning goes (stderr by default, resolved at call
            time so a redirected stderr actually receives it).
    """
    if not table_key:
        return
    columns = {name for name, _ in TABLE_COLUMNS}
    unknown = sorted(
        key for key in table_key if key != "heading" and key not in columns
    )
    if unknown:
        print(
            "warning: table_key entries with no matching table column "
            f"(ignored): {', '.join(unknown)}",
            file=stream if stream is not None else sys.stderr,
        )


def load_table_key(path: Path) -> dict[str, str] | None:
    """Read the ``table_key`` block out of a study manifest.

    The report stage is also usable on its own, without the runner, so a
    missing manifest is simply "no key supplied" -- the English defaults take
    over. A manifest that *is* there but is malformed must stop the run: a
    key built from silently-coerced values would be worse than no key.

    Args:
        path: Manifest to read; absent yields None.

    Returns:
        The block as ``column -> text``, or None when there is nothing to use.

    Raises:
        SystemExit: If the manifest cannot be read, or ``table_key`` is not
            an object of strings.
    """
    if not path.is_file():
        return None
    study = common.read_json(path)
    block = study.get("table_key") if isinstance(study, dict) else None
    if block is None:
        return None
    if not isinstance(block, dict):
        raise SystemExit(
            f"error: 'table_key' in {path} must be an object mapping column "
            "names to definitions"
        )
    bad = [key for key, value in block.items() if not isinstance(value, str)]
    if bad:
        raise SystemExit(
            f"error: table_key values in {path} must be strings "
            f"(bad: {', '.join(str(key) for key in bad)})"
        )
    return {str(key): value for key, value in block.items()}


def table_key_block(
    table_key: Mapping[str, str] | None = None,
    translator: i18n.Translator | None = None,
) -> str:
    """HTML for the key under the table (``""`` when no column needs one).

    Args:
        table_key: The manifest block (see :func:`table_key_items`).
        translator: Target language; English when omitted.

    Returns:
        A ``<section class="table-key">`` block, or an empty string when
        :func:`table_key_items` has nothing to explain.
    """
    tr = translator or i18n.english()
    items = table_key_items(table_key, tr)
    if not items:
        return ""
    heading = html.escape(table_key_heading(table_key, tr))
    entries = "".join(
        f"<dt>{html.escape(code)}</dt><dd>{html.escape(definition)}</dd>"
        for code, definition in items
    )
    return f'<section class="table-key"><h3>{heading}</h3><dl>{entries}</dl></section>'


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
    css = grade if grade in GRADE_PILLS else "gap"
    return f'<span class="pill pill-{css}">{html.escape(label)}</span>'


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


def sparkline_svg(
    values: list[float],
    colour: str,
    alt: str,
    width: int = 160,
    height: int = 30,
) -> str:
    """A tiny inline SVG line chart of ``values`` (self-contained, no scripts).

    Args:
        values: The monthly series to plot (share per million).
        colour: Hex stroke colour; also used for the translucent area fill.
        alt: Accessible description (translated by the caller).
        width: Viewport width in user units.
        height: Viewport height in user units.

    Returns:
        An ``<svg>`` string, or ``""`` when there is nothing honest to draw
        (fewer than two points cannot show a trend).
    """
    if len(values) < 2:
        return ""
    lo, hi = min(values), max(values)
    span = (hi - lo) or 1.0
    pad = 2.0
    step = (width - 2 * pad) / (len(values) - 1)
    points = [
        (
            pad + index * step,
            height - pad - ((value - lo) / span) * (height - 2 * pad),
        )
        for index, value in enumerate(values)
    ]
    line = " ".join(f"{x:.1f},{y:.1f}" for x, y in points)
    area = f"{pad:.1f},{height - pad:.1f} {line} {width - pad:.1f},{height - pad:.1f}"
    label = html.escape(alt)
    return (
        f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="{label}" '
        f'preserveAspectRatio="none">'
        f'<polygon points="{area}" fill="{colour}" opacity="0.13"/>'
        f'<polyline points="{line}" fill="none" stroke="{colour}" '
        f'stroke-width="1.6" stroke-linejoin="round" stroke-linecap="round"/>'
        f"</svg>"
    )


def kpi_tiles_for(
    analysis: AnalysisPayload, translator: i18n.Translator | None = None
) -> str:
    """The KPI tile grid: one card per measured language, then gap cards.

    Each tile answers "how big, which way, how much do we trust it" without
    reading the table: a big YoY-share number with an arrow, the two
    supporting counts, a confidence pill and a sparkline of the real monthly
    share series. Coverage gaps get their own dashed card, so a missing
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
        sparkline = sparkline_svg(
            list(series["share_ppm"]) if series else [],
            _colour_for(analysis, language),
            tr.t("report.sparkline_alt", lang=language),
        )
        tiles.append(
            '<article class="tile" style="--series:{}">'
            '<div class="tile-head">'
            '<span class="tile-lang">{}</span>{}</div>'
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
                confidence_pill_html(
                    metric["confidence"],
                    tr.t(f"confidence.{metric['confidence']}").upper(),
                ),
                html.escape(metric["article_title"]),
                kind,
                trend_arrow(kind),
                html.escape(fmt_pct(share_pct, tr)),
                html.escape(tr.t("report.kpi_yoy")),
                f"{metric['article_total']:,}",
                html.escape(tr.t("report.kpi_views")),
                f"{metric['share_ppm']:.2f}",
                html.escape(tr.t("report.kpi_share")),
                sparkline,
            )
        )
    for language in analysis.get("gaps", []):
        tiles.append(
            '<article class="tile gap-tile">'
            '<div class="tile-head">'
            f'<span class="tile-lang">{html.escape(language)}</span>'
            f"{confidence_pill_html('gap', tr.t('report.gap_confidence'))}</div>"
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


def _table_cell(name: str, row: dict[str, str]) -> str:
    """One ``<td>``; styled by role, with gap markers preserved.

    YoY cells get the trend class (green/red/grey) so direction reads at a
    glance, and the Confidence cell becomes a pill -- both purely presentational:
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
    if name in {"YoY", "YoY share"}:
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
    *,
    table_key: Mapping[str, str] | None = None,
) -> None:
    """Write the self-contained HTML report (chart embedded as base64).

    Args:
        analysis: The ``analysis.json`` payload.
        chart_png: Chart to embed; a missing/absent file degrades to a
            placeholder note rather than breaking the report.
        out_path: Output file; parent directories are created.
        translator: Target language; English when omitted.
        table_key: The manifest's ``table_key`` block -- the agent-written
            decoding printed under the table. Absent means the English
            defaults, recorded as untranslated in a localised report.

    Raises:
        OSError: If the template or output cannot be read/written.
        KeyError: If a placeholder's section is missing from the payload.
    """
    tr = translator or i18n.english()
    template = string.Template(TEMPLATE_PATH.read_text(encoding="utf-8"))

    if chart_png and chart_png.is_file():
        encoded = base64.b64encode(chart_png.read_bytes()).decode("ascii")
        chart_block = (
            f'<figure><img alt="{html.escape(tr.t("report.chart_alt"))}" '
            f'src="data:image/png;base64,{encoded}"/>'
            f"<figcaption>{html.escape(tr.t('report.chart_caption'))}"
            "</figcaption></figure>"
        )
    else:
        chart_block = (
            f'<figure class="gap">{html.escape(tr.t("report.chart_missing"))}'
            "</figure>"
        )

    header_cells = "".join(
        f"<th>{html.escape(name)}</th>"
        for name, _ in TABLE_COLUMNS
    )
    body_rows = [
        _table_row(row) for row in table_rows_for(analysis, tr)
    ]

    table_block = (
        "<table><thead><tr>"
        + header_cells
        + "</tr></thead><tbody>"
        + "".join(body_rows)
        + "</tbody></table>"
    )
    # The key sits with the table it decodes, not with the caveats: a reader
    # who does not know what "YoY" means should not have to scroll for it.
    legend_block = table_key_block(table_key, tr)

    bullets = "".join(
        f"<li>{html.escape(item)}</li>" for item in caveat_items(analysis, tr)
    )

    # Everything the reader sees is rendered first: the note counts the
    # strings that fell back to English, so it is only accurate afterwards.
    title = tr.t("report.title", topic=str(analysis["topic"]))
    subtitle = subtitle_for(analysis, tr)
    kicker = tr.t("report.kicker")
    verdict_tag = tr.t("report.verdict_tag")
    heading = tr.t("report.assumptions_heading")
    footer = footer_for(analysis, tr)
    headline = headline_for(analysis, tr)
    gap_note = gaps_note(analysis, tr)
    kpi_block = kpi_tiles_for(analysis, tr)
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
        kpi_block=kpi_block,
        chart_block=chart_block,
        table_block=table_block,
        legend_block=legend_block,
        heading=html.escape(heading),
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
    if name in {"YoY", "YoY share"}:
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
    """Draw the confidence badge as a rounded, filled pill.

    Args:
        writer: Canvas to draw on.
        grade: ``high``/``medium``/``low``/``gap`` -- selects the colours;
            an unknown grade degrades to the ``gap`` palette.
        label: Already-translated badge text.
        x: Column left edge.
        width: Column width.
        row_y: Row bottom (the cursor position for this row).
        row_height: Row height, so the pill centres vertically in it.
    """
    bg, fg = GRADE_PILLS.get(grade, GRADE_PILLS["gap"])
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
    writer.setFillColor(bg)
    writer.roundRect(pill_x, pill_y, pill_w, pill_h, pill_h / 2, stroke=0, fill=1)
    writer.setFillColor(fg)
    writer.setFont(FONT_BOLD, size)
    writer.drawCentredString(pill_x + pill_w / 2, pill_y + 2.6, label)


def render_pdf(
    analysis: AnalysisPayload,
    chart_png: Path | None,
    out_path: Path,
    translator: i18n.Translator | None = None,
    *,
    table_key: Mapping[str, str] | None = None,
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
        table_key: The manifest's ``table_key`` block -- the agent-written
            decoding printed under the table. Absent means the English
            defaults, recorded as untranslated in a localised report.

    Raises:
        ReportOverflow: When any block would cross the bottom margin.
    """
    tr = translator or i18n.english()
    writer = pdf_canvas.Canvas(str(out_path), pagesize=letter)
    title = tr.t("report.title", topic=str(analysis["topic"]))
    writer.setTitle(title)
    writer.setAuthor("wikipedia-interest-analyzer")
    sheet = _Sheet(writer)

    # --- header: accent band with white title (replaces the thin rule) ------
    # The band is decoration only: it wraps the same two blocks the old header
    # printed, so the vertical budget grows only by its padding.
    subtitle = subtitle_for(analysis, tr)
    title_lines = wrap(title, FONT_BOLD, 17, CONTENT_W - 20)
    subtitle_lines = wrap(subtitle, FONT, 8, CONTENT_W - 20)
    band_height = 8 + 21 * len(title_lines) + 11 * len(subtitle_lines) + 8
    sheet.need(band_height + 10, "header band")
    band_top = sheet.y + 6
    writer.setFillColor(ACCENT)
    band_bottom = band_top - band_height
    writer.rect(MARGIN, band_bottom, CONTENT_W, band_height, stroke=0, fill=1)
    cursor = band_top - 8
    writer.setFont(FONT_BOLD, 17)
    for line in title_lines:
        cursor -= 21
        writer.setFillColor(WHITE)
        writer.drawString(MARGIN + 10, cursor, line)
    writer.setFont(FONT, 8)
    for line in subtitle_lines:
        cursor -= 11
        writer.setFillColor(SUBTITLE_ON_DARK)
        writer.drawString(MARGIN + 10, cursor, line)
    writer.setFillColor(GOLD)
    writer.rect(MARGIN, band_top - band_height, CONTENT_W, 2, stroke=0, fill=1)
    sheet.y = band_top - band_height - 10

    # --- verdict: dark panel, gold rule and tag ----------------------------
    # ``headline`` already ends with the coverage-gap sentence when there are
    # gaps (analyze.py appends it), so the PDF prints it exactly once -- the
    # old second copy cost one-page space and read as a stutter.
    verdict = headline_for(analysis, tr)
    tag = tr.t("report.verdict_tag").upper()
    verdict_lines = wrap(verdict, FONT, 9.5, CONTENT_W - 22)
    tag_width = pdfmetrics.stringWidth(tag, FONT_BOLD, 7.0) + 12
    box_height = 14 + 12.5 * len(verdict_lines) + 16
    sheet.need(box_height + 10, "verdict box")
    box_top = sheet.y
    writer.setFillColor(ACCENT_DEEP)
    writer.rect(MARGIN, box_top - box_height, CONTENT_W, box_height, stroke=0, fill=1)
    writer.setFillColor(GOLD)
    writer.rect(MARGIN, box_top - box_height, 3, box_height, stroke=0, fill=1)
    # Tag chip: sits at the top-left of the panel, above the verdict text.
    writer.setFillColor(GOLD)
    writer.roundRect(
        MARGIN + 11, box_top - 12, tag_width, 11, 5.5, stroke=0, fill=1
    )
    writer.setFillColor(ACCENT_DEEP)
    writer.setFont(FONT_BOLD, 7.0)
    writer.drawCentredString(MARGIN + 11 + tag_width / 2, box_top - 9.5, tag)
    writer.setFillColor(WHITE)
    writer.setFont(FONT, 9.5)
    cursor = box_top - 22
    for line in verdict_lines:
        cursor -= 12.5
        writer.drawString(MARGIN + 11, cursor, line)
    sheet.y = box_top - box_height - 12

    # --- chart -------------------------------------------------------------
    if chart_png and chart_png.is_file():
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
    sheet.need(16 + row_height * len(rows) + 8, "comparison table")

    x_positions = []
    cursor_x = MARGIN
    for _, width in TABLE_COLUMNS:
        x_positions.append(cursor_x)
        cursor_x += width

    header_top = sheet.y
    writer.setFillColor(ACCENT)
    writer.rect(MARGIN, header_top - 16, CONTENT_W, 16, stroke=0, fill=1)
    writer.setFont(FONT_BOLD, 7.8)
    writer.setFillColor(WHITE)
    for (name, width), x in zip(TABLE_COLUMNS, x_positions, strict=True):
        # A translated header can be longer than its fixed column: shorten it
        # rather than let it collide with the neighbour it shares a row with.
        value = fit(name, FONT_BOLD, 7.8, width - 8)
        if name in ALIGN_LEFT:
            writer.drawString(x + 4, header_top - 11.5, value)
        else:
            right = x + width - 4
            writer.drawRightString(right, header_top - 11.5, value)
    sheet.y = header_top - 16

    for index, row in enumerate(rows):
        sheet.y -= row_height
        if row["gap"]:
            writer.setFillColor(GAP_ROW_BG)
            writer.rect(MARGIN, sheet.y, CONTENT_W, row_height, stroke=0, fill=1)
        elif index % 2:
            writer.setFillColor(ZEBRA)
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
        writer.setStrokeColor(RULE)
        writer.setLineWidth(0.5)
        writer.line(MARGIN, sheet.y, PAGE_W - MARGIN, sheet.y)
    sheet.gap(8)

    # --- key under the table ------------------------------------------------
    # The headers are short codes; their wording is drawn here, a few points
    # below the grid, so a reader meets the decoding while the table is still
    # in view rather than down among the caveats.
    key_items = table_key_items(table_key, tr)
    if key_items:
        sheet.gap(2)
        sheet.text(
            table_key_heading(table_key, tr),
            FONT_BOLD,
            7.5,
            9.5,
            color=ACCENT,
            what="table key heading",
        )
        for code, definition in key_items:
            sheet.text(
                f"{code} \u2014 {definition}",
                FONT,
                7.0,
                8.8,
                indent=8,
                color=MUTED,
                what="table key entry",
            )
        sheet.gap(4)

    # --- assumptions & limitations ----------------------------------------
    heading = tr.t("report.assumptions_heading").upper()
    sheet.need(12, "limitations heading")
    writer.setFillColor(GOLD)
    writer.rect(MARGIN, sheet.y - 10, 3, 10, stroke=0, fill=1)
    sheet.text(
        heading,
        FONT_BOLD,
        8.5,
        12,
        color=ACCENT,
        x=MARGIN + 9,
        max_width=CONTENT_W - 9,
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
            color=HexColor("#a11111"),
            what="untranslated note",
        )

    # --- footer ------------------------------------------------------------
    sheet.gap(6)
    writer.setStrokeColor(RULE)
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
        "--study",
        default="study.json",
        help="study manifest carrying the table_key block (optional)",
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

    # The decoding under the table comes from the manifest, never from the
    # translation catalogue: it is generated once per study, not translated.
    table_key = load_table_key(Path(args.study))
    warn_unknown_table_key(table_key)

    lang = i18n.normalize_lang(args.report_lang)
    fallback = Path(f"translations.{lang}.json")
    translations = Path(args.translations) if args.translations else fallback
    translator = i18n.translator_for(lang, translations)

    html_path = Path(args.html)
    render_html(analysis, chart, html_path, translator, table_key=table_key)
    print(f"wrote {html_path}")

    status = 0
    pdf_path = Path(args.pdf)
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        render_pdf(analysis, chart, pdf_path, translator, table_key=table_key)
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
