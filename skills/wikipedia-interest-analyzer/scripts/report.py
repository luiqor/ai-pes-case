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

ACCENT = HexColor("#1f4e79")
MUTED = HexColor("#666666")
RULE = HexColor("#d9d9d9")
VERDICT_BG = HexColor("#f3f7fb")
HEADER_BG = HexColor("#f0f0f0")
INK = HexColor("#1a1a1a")

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
        Row dicts keyed by column name plus the ``"gap"`` marker.
    """
    tr = translator or i18n.english()
    rows: list[dict[str, str]] = []
    for language, metric in analysis["metrics"].items():
        yoy = metric["yoy"]
        rows.append(
            {
                "gap": "",
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
    """One ``<td>``; a gap row's Article cell gets the ``gap`` marker span."""
    value = html.escape(row[name])
    if row["gap"] and name == "Article":
        return f'<td><span class="gap">{value}</span></td>'
    return f"<td>{value}</td>"


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
        f"<th>{html.escape(tr.t(COLUMN_MESSAGES[name]))}</th>"
        for name, _ in TABLE_COLUMNS
    )
    body_rows = [
        "<tr>" + "".join(_table_cell(name, row) for name, _ in TABLE_COLUMNS) + "</tr>"
        for row in table_rows_for(analysis, tr)
    ]

    table_block = (
        "<table><thead><tr>"
        + header_cells
        + "</tr></thead><tbody>"
        + "".join(body_rows)
        + "</tbody></table>"
    )

    bullets = "".join(
        f"<li>{html.escape(item)}</li>" for item in caveat_items(analysis, tr)
    )

    # Everything the reader sees is rendered first: the note counts the
    # strings that fell back to English, so it is only accurate afterwards.
    title = tr.t("report.title", topic=str(analysis["topic"]))
    subtitle = subtitle_for(analysis, tr)
    heading = tr.t("report.assumptions_heading")
    footer = footer_for(analysis, tr)
    headline = headline_for(analysis, tr)
    gap_note = gaps_note(analysis, tr)
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
        h1=html.escape(title),
        subtitle=html.escape(subtitle),
        headline=html.escape(headline) + gap_note,
        chart_block=chart_block,
        table_block=table_block,
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

    # --- header ------------------------------------------------------------
    sheet.text(title, FONT_BOLD, 17, 21, what="report title")
    sheet.text(
        subtitle_for(analysis, tr), FONT, 8, 11, color=MUTED, what="subtitle"
    )
    sheet.gap(3)
    writer.setStrokeColor(ACCENT)
    writer.setLineWidth(2.2)
    writer.line(MARGIN, sheet.y, PAGE_W - MARGIN, sheet.y)
    sheet.gap(10)

    # --- verdict -----------------------------------------------------------
    # ``headline`` already ends with the coverage-gap sentence when there are
    # gaps (analyze.py appends it), so the PDF prints it exactly once -- the
    # old second copy cost one-page space and read as a stutter.
    verdict = headline_for(analysis, tr)
    verdict_lines = wrap(verdict, FONT, 9.5, CONTENT_W - 22)
    box_height = 12.5 * len(verdict_lines) + 16
    sheet.need(box_height + 10, "verdict box")
    box_top = sheet.y
    writer.setFillColor(VERDICT_BG)
    writer.rect(MARGIN, box_top - box_height, CONTENT_W, box_height, stroke=0, fill=1)
    writer.setFillColor(ACCENT)
    writer.rect(MARGIN, box_top - box_height, 3, box_height, stroke=0, fill=1)
    writer.setFillColor(INK)
    writer.setFont(FONT, 9.5)
    cursor = box_top - 8
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
    writer.setFillColor(HEADER_BG)
    writer.rect(MARGIN, header_top - 16, CONTENT_W, 16, stroke=0, fill=1)
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

    for row in rows:
        sheet.y -= row_height
        writer.setFont(FONT, 7.8)
        writer.setFillColor(HexColor("#a11111") if row["gap"] else INK)
        for (name, width), x in zip(TABLE_COLUMNS, x_positions, strict=True):
            value = row[name]
            if name in ALIGN_LEFT:
                writer.drawString(x + 4, sheet.y + 3.5, value)
            else:
                writer.drawRightString(x + width - 4, sheet.y + 3.5, value)
        writer.setStrokeColor(RULE)
        writer.setLineWidth(0.5)
        writer.line(MARGIN, sheet.y, PAGE_W - MARGIN, sheet.y)
    sheet.gap(8)

    # --- assumptions & limitations ----------------------------------------
    sheet.text(
        tr.t("report.assumptions_heading").upper(),
        FONT_BOLD,
        8.5,
        12,
        color=ACCENT,
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
