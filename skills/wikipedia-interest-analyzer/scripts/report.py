"""Produce the shareable report: a self-contained HTML file and a matching PDF.

The two renderers are built from the same data builders, so they cannot
disagree about *what* the report says: the PDF carries every block the HTML
does (masthead, verdict, KPI tiles, chart, table, key, success criteria,
caveats, footer).

Layout is driven by an explicit vertical cursor, and every block asks for the
space it needs before drawing. The cursor breaks a page whenever the next
block would cross the bottom margin, so a long report simply continues on a
second page -- nothing is dropped to make it fit. ``ReportOverflow`` is
reserved for a block that would not fit on an *empty* page; in that case no
PDF is written and the caller is told which block is impossible.

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
import math
import string
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TextIO, TypedDict

import common
import i18n
from payloads import (
    AnalysisPayload,
    CriteriaResult,
    CriterionVerdict,
    MessageRef,
    SuccessRule,
)
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

#: KPI tile grid geometry for the PDF -- the same grid the HTML template
#: builds with ``repeat(auto-fit, minmax(150px, 1fr))``: a tile is never
#: narrower than ``KPI_MIN_W`` (150px in points), never more than
#: ``KPI_MAX_COLS`` sit side by side, and the rest flow into further rows
#: instead of being squeezed.
KPI_MIN_W = 112.5
KPI_MAX_COLS = 4
KPI_GAP = 8.0
KPI_TILE_H = 116.0

#: The **base** printed column headers and their widths in points; the widths
#: add up to ``CONTENT_W`` exactly, so the grid fills the page edge to edge.
#:
#: The labels are **not translatable** -- a metric name is a code, and a
#: column this narrow cannot hold a translated phrase anyway. The decoding a
#: reader needs is printed under the table from the manifest's ``table_key``
#: block (:func:`table_key_items`), never from the translation catalogue.
#:
#: Columns for the optional data layers are *appended* by
#: :func:`table_columns_for` when the analysis carries those metrics, and the
#: widths are re-fitted to ``CONTENT_W`` -- a study without layers renders
#: exactly this grid, byte for byte.
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

#: Optional columns, keyed by the metric that must be present in at least
#: one language for the column to appear (name -> relative width weight).
#: The weights are normalised to ``CONTENT_W`` together with the base grid;
#: with no optional column active they are never used, so the base widths
#: above stay exact.
OPTIONAL_TABLE_COLUMNS: dict[str, int] = {
    "Mobile %": 54,
    "Bot %": 42,
    "Rank": 38,
}

#: Columns whose header is an abbreviation: these must be defined under the
#: table, so each ships with an English fallback (:data:`TABLE_KEY_DEFAULTS`).
#: The plain columns ("Lang", "Article", "Views", "Confidence") read as their
#: own definition and need no entry -- an agent may still supply one.
#: Only columns that are actually printed are ever listed (see
#: :func:`table_key_items`), so the optional codes cost nothing in a study
#: that never fetched their layer.
TABLE_KEY_COLUMNS = ("Share/M", "YoY", "YoY share", "R\u00b2", *OPTIONAL_TABLE_COLUMNS)

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
    "Mobile %": (
        "Share of this article's window views read on mobile (mobile-web + "
        "mobile-app), in percent."
    ),
    "Bot %": (
        "Non-user share (spider + automated) of all-agents views; lower means "
        "a cleaner human signal."
    ),
    "Rank": (
        "Placement in the project's monthly top list at the window's end; "
        ">N means outside the listed top N."
    ),
}


class ReportOverflow(RuntimeError):
    """Raised when a block cannot be printed even on a whole empty page."""


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


def _fit_widths(columns: list[tuple[str, int]]) -> list[tuple[str, int]]:
    """Scale column weights so the grid still fills ``CONTENT_W`` exactly.

    The base grid already sums to ``CONTENT_W`` and is returned untouched;
    when optional columns join, every column gives up points in proportion to
    how much room it has above its own header (no column is ever pushed below
    the width its header needs), and the rounding remainder is settled on the
    widest column so the sum is exact rather than merely close.

    Args:
        columns: ``(header, weight)`` pairs; total need not be ``CONTENT_W``.

    Returns:
        The same headers with weights summing to exactly ``CONTENT_W``.

    Raises:
        SystemExit: If the deficit exceeds all available slack -- impossible
            with the shipped columns, but a loud failure beats a PDF whose
            headers collide silently.
    """
    total = sum(width for _, width in columns)
    target = int(CONTENT_W)
    if total == target:
        return columns
    minima = [
        (name, math.ceil(pdfmetrics.stringWidth(name, FONT_BOLD, 7.8)) + 9)
        for name, _ in columns
    ]
    slack = [
        width - minimum
        for (_, width), (_, minimum) in zip(columns, minima, strict=True)
    ]
    deficit = total - target
    if deficit > 0:
        pool = sum(slack)
        if pool < deficit:
            raise SystemExit(
                "error: the table columns do not fit the page width "
                f"({total}pt of weight, only {pool}pt of slack above the "
                f"headers for a {deficit}pt deficit)"
            )
        scaled = [
            (name, width - deficit * (room / pool))
            for (name, width), room in zip(columns, slack, strict=True)
        ]
    else:
        gain = -deficit
        scaled = [
            (name, width + gain * (width / total))
            for name, width in columns
        ]
    rounded = [(name, round(width)) for name, width in scaled]
    delta = target - sum(width for _, width in rounded)
    if delta:
        widest = max(range(len(rounded)), key=lambda i: rounded[i][1])
        name, width = rounded[widest]
        rounded[widest] = (name, width + delta)
    return rounded


def table_columns_for(analysis: AnalysisPayload) -> list[tuple[str, int]]:
    """The printed columns for this analysis: the base grid, plus layer columns.

    A layer column appears only when at least one language actually carries
    its metric -- a report never shows an empty column, and a study without
    ``--layers`` renders exactly the base grid the catalogue tests pin down.

    Args:
        analysis: The ``analysis.json`` payload.

    Returns:
        ``(header, width)`` pairs summing to ``CONTENT_W``, in print order.
    """
    columns = list(TABLE_COLUMNS)
    metrics = analysis.get("metrics") or {}
    if any(metric.get("access_split") for metric in metrics.values()):
        columns.append(("Mobile %", OPTIONAL_TABLE_COLUMNS["Mobile %"]))
    if any("bot_share_pct" in metric for metric in metrics.values()):
        columns.append(("Bot %", OPTIONAL_TABLE_COLUMNS["Bot %"]))
    if any(metric.get("top_rank") for metric in metrics.values()):
        columns.append(("Rank", OPTIONAL_TABLE_COLUMNS["Rank"]))
    return _fit_widths(columns)


def _measured_order(analysis: AnalysisPayload) -> list[str]:
    """Language order for the table: the study's ranking first, then the rest.

    When the study picked a ``rank_by`` criterion the comparison already
    sorted the languages; the table follows it so the report's centrepiece
    and its ranking note agree. Languages the ranking skipped keep their
    original order after the ranked ones.

    Args:
        analysis: The ``analysis.json`` payload.

    Returns:
        Measured language codes in display order.
    """
    metrics = analysis["metrics"]
    ranked = (analysis.get("comparison") or {}).get("ranked_by")
    if not ranked:
        return list(metrics)
    listed = [code for code in ranked["order"] if code in metrics]
    seen = set(listed)
    return listed + [code for code in metrics if code not in seen]


def table_rows_for(
    analysis: AnalysisPayload,
    translator: i18n.Translator | None = None,
    *,
    columns: list[tuple[str, int]] | None = None,
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
        columns: Active columns from :func:`table_columns_for`; defaults to
            the base grid so a caller that does not care about layers keeps
            the historical rows. The optional layer cells are only built when
            their column is active.

    Returns:
        Row dicts keyed by every base column (plus any active optional one),
        plus the ``"gap"`` marker and the ``"grade"`` (confidence pill class)
        presentation hint. A layer metric this language never produced is
        rendered as an em dash -- never as a blank, never as zero.
    """
    tr = translator or i18n.english()
    names = {name for name, _ in columns} if columns is not None else None

    def wants(name: str) -> bool:
        return names is None or name in names

    rows: list[dict[str, str]] = []
    for language in _measured_order(analysis):
        metric = analysis["metrics"][language]
        yoy = metric["yoy"]
        row: dict[str, str] = {
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
        if wants("Mobile %"):
            split = metric.get("access_split")
            row["Mobile %"] = (
                f"{split['mobile_pct']:.1f}%" if split else "\u2014"
            )
        if wants("Bot %"):
            bot = metric.get("bot_share_pct")
            row["Bot %"] = f"{bot:.1f}%" if bot is not None else "\u2014"
        if wants("Rank"):
            top = metric.get("top_rank")
            if top is None:
                row["Rank"] = "\u2014"
            elif top["rank"] is None:
                row["Rank"] = f">{top['list_size']}"
            else:
                row["Rank"] = str(top["rank"])
        rows.append(row)
    for language in analysis.get("gaps", []):
        row = {
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
        for name in ("Mobile %", "Bot %", "Rank"):
            if wants(name):
                row[name] = "\u2014"
        rows.append(row)
    return rows


def ranked_by_text(
    analysis: AnalysisPayload, translator: i18n.Translator | None = None
) -> str:
    """One plain-text line saying the table is sorted by the chosen criterion.

    Without this line a reordered table is a puzzle: the reader has no way to
    see *why* one language leads. A study with no ``rank_by`` gets an empty
    string -- the default order needs no excuse. Plain text so both the HTML
    and the PDF can print the same sentence.

    Args:
        analysis: The ``analysis.json`` payload.
        translator: Target language; English when omitted.

    Returns:
        ``""`` or a sentence naming the criterion, e.g.
        ``Ranked by share of edition reading (per million), best first.``
    """
    ranked = (analysis.get("comparison") or {}).get("ranked_by")
    if not ranked:
        return ""
    tr = translator or i18n.english()
    label = tr.t(f"criterion.{ranked['criterion']}")
    return tr.t("report.ranked_by", criterion=label)


def _criteria_of(analysis: AnalysisPayload) -> CriteriaResult | None:
    """The ``criteria`` block, or ``None`` when the study asked for no rules."""
    criteria = analysis.get("criteria")
    if not criteria or not criteria.get("success"):
        return None
    return criteria


def _rule_text(rule: SuccessRule, tr: i18n.Translator) -> str:
    """Human wording of one rule: the manifest's own label, else metric+op+value."""
    label = rule.get("label")
    if label:
        return str(label)
    value = rule["value"]
    if isinstance(value, str):
        shown = tr.t(f"confidence.{value}")
    else:
        shown = f"{float(value):g}"
    return f"{tr.t('metric.' + str(rule['metric']))} {rule['op']} {shown}"


def _glyph(passed: bool | None) -> str:
    """Verdict marks: measured pass, measured fail, or not measurable."""
    if passed is True:
        return "\u2713"
    if passed is False:
        return "\u2717"
    return "\u2014"


def _verdict_glyph(verdicts: dict[str, CriterionVerdict], rule_id: str) -> str:
    """The mark for one rule in one language's verdict map."""
    verdict = verdicts.get(rule_id)
    if verdict is None:
        return "\u2014"
    return _glyph(verdict.get("passed"))


def criteria_summaries(
    analysis: AnalysisPayload, translator: i18n.Translator | None = None
) -> list[str]:
    """One short ``pl 2/3 met`` string per language; gaps called out.

    Args:
        analysis: The ``analysis.json`` payload.
        translator: Target language; English when omitted.

    Returns:
        The per-language summaries in verdict order (measured languages
        first, coverage gaps last), or ``[]`` without criteria.
    """
    criteria = _criteria_of(analysis)
    if not criteria:
        return []
    tr = translator or i18n.english()
    gaps = set(analysis.get("gaps") or [])
    summaries: list[str] = []
    for language in criteria["verdicts"]:
        if language in gaps:
            summaries.append(tr.t("criteria.lang_gap", lang=language))
            continue
        counts = criteria["summary"][language]
        if counts["not_evaluable"]:
            summaries.append(
                tr.t(
                    "criteria.lang_summary_na",
                    lang=language,
                    met=counts["met"],
                    total=counts["total"],
                    na=counts["not_evaluable"],
                )
            )
        else:
            summaries.append(
                tr.t(
                    "criteria.lang_summary",
                    lang=language,
                    met=counts["met"],
                    total=counts["total"],
                )
            )
    return summaries


def criteria_view(
    analysis: AnalysisPayload, translator: i18n.Translator | None = None
) -> tuple[str, list[str], str] | None:
    """The success-criteria block as plain strings: heading, rules, summary.

    One builder, two renderers: the HTML escapes these strings into its
    ``<section class="criteria">`` and the PDF draws the very same lines, so
    the two artifacts can never grade the user's rules differently.

    Values are deliberately not printed -- units differ per metric (views,
    per-million share, a grade) and the numbers live in ``analysis.json``;
    the report shows the verdict the user asked for.

    Args:
        analysis: The ``analysis.json`` payload.
        translator: Target language; English when omitted.

    Returns:
        ``(heading, rule lines, summary)``, or ``None`` when the study asked
        for no rules (both renderers then print nothing).
    """
    criteria = _criteria_of(analysis)
    if not criteria:
        return None
    tr = translator or i18n.english()
    rules: list[str] = []
    for rule in criteria["success"]:
        results = [
            f"{language} {_verdict_glyph(verdicts, str(rule['id']))}"
            for language, verdicts in criteria["verdicts"].items()
        ]
        rules.append(
            tr.t(
                "criteria.rule",
                rule=_rule_text(rule, tr),
                results=tr.t("join.comma").join(results),
            )
        )
    summary = tr.t(
        "criteria.summary",
        summaries=tr.t("join.comma").join(criteria_summaries(analysis, tr)),
    )
    return tr.t("criteria.heading"), rules, summary


def criteria_block_html(
    analysis: AnalysisPayload, translator: i18n.Translator | None = None
) -> str:
    """The HTML block: every rule with each language's mark, then the summary.

    Wording comes from :func:`criteria_view`; this function only escapes it
    and wraps it in the markup the template's CSS styles.
    """
    view = criteria_view(analysis, translator)
    if not view:
        return ""
    heading, rules, summary = view
    rows = "".join(f"<li>{html.escape(rule)}</li>" for rule in rules)
    return (
        f'<section class="criteria"><h3>{html.escape(heading)}</h3>'
        f"<ul>{rows}</ul>"
        f'<p class="criteria-summary">{html.escape(summary)}</p></section>'
    )


def gaps_note_text(
    analysis: AnalysisPayload, translator: i18n.Translator | None = None
) -> str:
    """The plain sentence naming languages with no article (``""`` if none).

    One wording for both renderers: the HTML wraps it in a ``<span>``, the
    PDF appends it to the verdict panel.
    """
    tr = translator or i18n.english()
    gaps = analysis.get("gaps", [])
    if not gaps:
        return ""
    return tr.t(
        "report.gaps_note",
        langs=tr.t("join.comma").join(gaps),
    )


def gaps_note(
    analysis: AnalysisPayload, translator: i18n.Translator | None = None
) -> str:
    """Inline HTML noting languages with no article (empty string if none)."""
    note = gaps_note_text(analysis, translator)
    if not note:
        return ""
    return f' <span class="gap">{html.escape(note)}</span>'


def table_key_items(
    table_key: Mapping[str, str] | None = None,
    translator: i18n.Translator | None = None,
    *,
    columns: list[tuple[str, int]] | None = None,
) -> list[tuple[str, str]]:
    """The ``(header, definition)`` pairs printed as the key under the table.

    Metric names are codes, so their decoding is *generated, not translated*:
    the wording comes from the study manifest's ``table_key`` block, written
    once per study by whoever runs the skill. A column in
    :data:`TABLE_KEY_COLUMNS` whose wording is missing falls back to
    :data:`TABLE_KEY_DEFAULTS` and is recorded on the translator, so a report
    that had to use the English fallback admits it instead of looking
    finished.

    Only columns that are actually printed are explained: an optional layer
    column that never appeared would push redundant wording into the report.

    Columns outside :data:`TABLE_KEY_COLUMNS` are printed only when the
    manifest supplies them -- they read as their own definition otherwise.

    Both renderers consume this list, so the HTML and PDF keys cannot
    disagree.

    Args:
        table_key: The manifest block (``column -> definition``), or None.
        translator: Target language; used only to record the English
            fallbacks, never to rewrite the supplied wording.
        columns: Active columns from :func:`table_columns_for`; the base grid
            when omitted.

    Returns:
        One pair per printed column that has something to say, in table order.
    """
    tr = translator or i18n.english()
    supplied = dict(table_key or {})
    active = columns if columns is not None else TABLE_COLUMNS
    items: list[tuple[str, str]] = []
    for name, _ in active:
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
    table_key: Mapping[str, str] | None,
    stream: TextIO | None = None,
    *,
    columns: list[tuple[str, int]] | None = None,
) -> None:
    """Warn about ``table_key`` entries naming a column the table never prints.

    A definition the report silently drops is work thrown away, so the run
    says so instead. Valid non-column keys (``heading``) are never flagged.

    Args:
        table_key: The manifest block, possibly None.
        stream: Where the warning goes (stderr by default, resolved at call
            time so a redirected stderr actually receives it).
        columns: Active columns; the base grid when omitted (so a layer
            definition is warned about whenever its column is not shown).
    """
    if not table_key:
        return
    active = columns if columns is not None else TABLE_COLUMNS
    printed = {name for name, _ in active}
    unknown = sorted(
        key for key in table_key if key != "heading" and key not in printed
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
    *,
    columns: list[tuple[str, int]] | None = None,
) -> str:
    """HTML for the key under the table (``""`` when no column needs one).

    Args:
        table_key: The manifest block (see :func:`table_key_items`).
        translator: Target language; English when omitted.
        columns: Active columns; the base grid when omitted.

    Returns:
        A ``<section class="table-key">`` block, or an empty string when
        :func:`table_key_items` has nothing to explain.
    """
    tr = translator or i18n.english()
    items = table_key_items(table_key, tr, columns=columns)
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


class KpiTile(TypedDict):
    """One KPI card's worth of data; the HTML and the PDF draw exactly this."""

    lang: str
    gap: bool
    grade: str
    pill: str
    title: str
    kind: str
    arrow: str
    hero: str
    hero_label: str
    stats: list[tuple[str, str]]
    sparkline: list[float]
    colour: str


def kpi_tiles_data(
    analysis: AnalysisPayload, translator: i18n.Translator | None = None
) -> list[KpiTile]:
    """The KPI tile model both renderers draw: one dict per tile, gaps last.

    Each tile answers "how big, which way, how much do we trust it" without
    reading the table: a big YoY-share number with an arrow, the two
    supporting counts, a confidence grade and the real monthly share series
    for a sparkline. Coverage gaps get their own tile, so a missing article
    stays visible instead of vanishing from the summary.

    Args:
        analysis: The ``analysis.json`` payload.
        translator: Target language; English when omitted.

    Returns:
        Tile dicts (empty when there are no metrics *and* no gaps). Keys:
        ``lang``, ``gap``, ``grade``, ``pill``, ``title``, ``kind``,
        ``arrow``, ``hero``, ``hero_label``, ``stats`` (label/value pairs),
        ``sparkline`` (share-per-million values) and ``colour``.
    """
    tr = translator or i18n.english()
    tiles: list[KpiTile] = []
    for language, metric in analysis["metrics"].items():
        yoy = metric["yoy"]
        share_pct = yoy.get("share_pct")
        kind = trend_class(share_pct)
        series = metric.get("series")
        tiles.append(
            {
                "lang": language,
                "gap": False,
                "grade": metric["confidence"],
                "pill": tr.t(f"confidence.{metric['confidence']}").upper(),
                "title": metric["article_title"],
                "kind": kind,
                "arrow": trend_arrow(kind),
                "hero": fmt_pct(share_pct, tr),
                "hero_label": tr.t("report.kpi_yoy"),
                "stats": [
                    (f"{metric['article_total']:,}", tr.t("report.kpi_views")),
                    (f"{metric['share_ppm']:.2f}", tr.t("report.kpi_share")),
                ],
                "sparkline": list(series["share_ppm"]) if series else [],
                "colour": _colour_for(analysis, language),
            }
        )
    for language in analysis.get("gaps", []):
        tiles.append(
            {
                "lang": language,
                "gap": True,
                "grade": "gap",
                "pill": tr.t("report.gap_confidence"),
                "title": tr.t("report.gap_article"),
                "kind": "flat",
                "arrow": "",
                "hero": tr.t("report.na"),
                "hero_label": tr.t("report.kpi_yoy"),
                "stats": [],
                "sparkline": [],
                "colour": common.SERIES_COLOURS[0],
            }
        )
    return tiles


def kpi_tiles_for(
    analysis: AnalysisPayload, translator: i18n.Translator | None = None
) -> str:
    """The HTML KPI tile grid built from :func:`kpi_tiles_data`.

    Args:
        analysis: The ``analysis.json`` payload.
        translator: Target language; English when omitted.

    Returns:
        HTML for a ``<section class="kpis">`` block (``""`` with no metrics
        *and* no gaps -- nothing to summarise).
    """
    tr = translator or i18n.english()
    tiles: list[str] = []
    for tile in kpi_tiles_data(analysis, tr):
        if tile["gap"]:
            tiles.append(
                '<article class="tile gap-tile">'
                '<div class="tile-head">'
                f'<span class="tile-lang">{html.escape(tile["lang"])}</span>'
                f"{confidence_pill_html('gap', tile['pill'])}</div>"
                f'<div class="tile-title">{html.escape(tile["title"])}</div>'
                '<div class="tile-hero flat"><span class="arrow" '
                f'aria-hidden="true"></span>{html.escape(tile["hero"])}</div>'
                f'<div class="tile-hero-label">{html.escape(tile["hero_label"])}</div>'
                "</article>"
            )
            continue
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
                tile["colour"],
                html.escape(tile["lang"]),
                confidence_pill_html(tile["grade"], tile["pill"]),
                html.escape(tile["title"]),
                tile["kind"],
                tile["arrow"],
                html.escape(tile["hero"]),
                html.escape(tile["hero_label"]),
                tile["stats"][0][0],
                html.escape(tile["stats"][0][1]),
                tile["stats"][1][0],
                html.escape(tile["stats"][1][1]),
                sparkline_svg(
                    tile["sparkline"],
                    tile["colour"],
                    tr.t("report.sparkline_alt", lang=tile["lang"]),
                ),
            )
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
    PDF could not tell that e.g. an unalignable window had capped the
    confidence grade. The report is the shareable artifact; it must carry them.
    Exact duplicates are dropped: a caveat stated twice is a caveat hedged
    twice, and it only pushes the rest further down the page.

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


def _table_row(
    row: dict[str, str], columns: list[tuple[str, int]] | None = None
) -> str:
    """One ``<tr>``; a coverage-gap row also carries the ``gap-row`` class.

    The class is what paints the row red/italic in the template's CSS, so a
    missing article stays visible as a *row*, not just as red cell text.
    """
    cls = ' class="gap-row"' if row["gap"] else ""
    active = columns if columns is not None else TABLE_COLUMNS
    cells = "".join(_table_cell(name, row) for name, _ in active)
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

    columns = table_columns_for(analysis)
    header_cells = "".join(
        f"<th>{html.escape(name)}</th>" for name, _ in columns
    )
    body_rows = [
        _table_row(row, columns)
        for row in table_rows_for(analysis, tr, columns=columns)
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
    # The ranking sentence goes first -- it explains the *order* of the rows
    # the reader is still looking at.
    ranked_text = ranked_by_text(analysis, tr)
    ranked_block = (
        f'<p class="ranked">{html.escape(ranked_text)}</p>' if ranked_text else ""
    )
    # The user's own pass/fail verdicts sit with the table they grade, after
    # the decoding -- a reader checks the criteria once the numbers are in view.
    criteria_block = criteria_block_html(analysis, tr)
    legend_block = (
        ranked_block + table_key_block(table_key, tr, columns=columns) + criteria_block
    )

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
# PDF -- the HTML's content, flowed over as many pages as it needs
# --------------------------------------------------------------------------
def pdf_glyphs(value: str) -> str:
    """Swap DejaVu-only marks for their Helvetica-safe lookalikes.

    ``✓``, ``✗``, ``▲`` and ``▼`` live in the font shipped with matplotlib,
    which is the normal case. When that font is unavailable the report falls
    back to Helvetica, whose glyph set has none of them -- and a reader who
    sees four black boxes instead of a verdict has learned nothing.

    Args:
        value: Text about to be drawn with :data:`FONT`.

    Returns:
        ``value`` unchanged when the Unicode font is registered, else with
        the four marks replaced by ASCII characters of the same meaning.
    """
    if FONT.startswith("WIA-"):
        return value
    return value.translate(
        {0x2713: ord("+"), 0x2717: ord("x"), 0x25B2: ord("^"), 0x25BC: ord("v")}
    )


class _Sheet:
    """Vertical layout cursor that breaks a page at the bottom margin.

    ``need`` closes the current sheet (footer first) and opens a fresh one
    whenever a block would cross the margin, so a long report continues on
    page two instead of being refused. :class:`ReportOverflow` survives for
    one situation only: a block that would not fit on an *empty* page -- then
    there is no honest way to print it, and the caller is told which block.
    """

    def __init__(
        self,
        writer: pdf_canvas.Canvas,
        page_footer: Callable[[int], None] | None = None,
    ) -> None:
        """Place the cursor at the top margin of ``writer``.

        Args:
            writer: The canvas being filled.
            page_footer: Optional ``f(page_number)`` called with the number of
                the page being closed, so every sheet carries the attribution
                line. It is *not* called for the last page -- the caller
                draws that one itself, right before ``showPage``.
        """
        self.writer = writer
        self._page_footer = page_footer
        self.y = PAGE_H - MARGIN
        self.pages = 1

    @property
    def at_top(self) -> bool:
        """True when nothing has been drawn on the current page yet."""
        return self.y >= PAGE_H - MARGIN - 0.01

    def new_page(self) -> None:
        """Close the current page (footer first) and start a fresh one."""
        if self._page_footer is not None:
            self._page_footer(self.pages)
        self.writer.showPage()
        self.pages += 1
        self.y = PAGE_H - MARGIN

    def need(self, height: float, what: str) -> bool:
        """Reserve ``height`` points, breaking a page first when needed.

        Args:
            height: Points the block needs.
            what: Block name, used in the overflow message.

        Returns:
            ``True`` when a page was broken (a caller repeating a header can
            redraw it), ``False`` when the block fits where it stands.

        Raises:
            ReportOverflow: When the block is taller than an empty page, or
                when breaking would print a blank sheet (nothing drawn yet
                and the block still does not fit).
        """
        if self.y - height >= BOTTOM:
            return False
        usable = PAGE_H - MARGIN - BOTTOM
        if height > usable:
            raise ReportOverflow(
                f"report cannot be printed: '{what}' needs {height:.0f}pt but "
                f"a whole page only offers {usable:.0f}pt of usable height. "
                "Shorten that block or split the study."
            )
        if self.at_top:
            raise ReportOverflow(
                f"report cannot be printed: '{what}' needs {height:.0f}pt and "
                f"nothing fits above it on an empty page ({usable:.0f}pt "
                "usable). Shorten that block or split the study."
            )
        self.new_page()
        return True

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
        """Wrap and draw one block, breaking a page wherever it runs out.

        Space is reserved per *line*, so a paragraph longer than the room
        left on a page simply starts again on the next one -- no block is
        ever dropped to keep the report short. Font and colour are re-asserted
        per line because breaking the sheet resets the canvas to its default
        Helvetica 12 in black.

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
        lines = wrap(pdf_glyphs(value), font, size, max_width - indent)
        for line in lines:
            self.need(leading, what)
            self.writer.setFont(font, size)
            self.writer.setFillColor(color)
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


def _kpi_columns(count: int) -> int:
    """How many KPI tiles stand side by side when there are ``count`` of them.

    Mirrors the HTML grid's own rules (``minmax(150px, 1fr)``): as many as
    fit at :data:`KPI_MIN_W` wide, never more than :data:`KPI_MAX_COLS`,
    never more than there are tiles -- the rest flow into further rows.

    Args:
        count: Tiles to place (0 still yields 1; the caller draws nothing).

    Returns:
        The column count for the grid.
    """
    if count < 2:
        return 1
    by_width = int((CONTENT_W + KPI_GAP) // (KPI_MIN_W + KPI_GAP))
    return max(1, min(count, KPI_MAX_COLS, by_width))


def _draw_sparkline(
    writer: pdf_canvas.Canvas,
    values: list[float],
    colour: str,
    x: float,
    y: float,
    width: float,
    height: float,
) -> None:
    """Draw a tile's month-by-month share line -- the HTML's inline SVG.

    Args:
        writer: Canvas to draw on.
        values: Monthly share-per-million values.
        colour: Hex stroke colour; its translucent copy fills the area under
            the line, exactly as the SVG does.
        x: Left edge of the drawing area.
        y: Bottom edge of the drawing area.
        width: Area width in points.
        height: Area height in points.

    Fewer than two points cannot show a trend, so nothing is drawn -- the
    same refusal the SVG builder makes.
    """
    if len(values) < 2:
        return
    lo, hi = min(values), max(values)
    span = (hi - lo) or 1.0
    pad = 2.0
    step = (width - 2 * pad) / (len(values) - 1)
    points = [
        (
            x + pad + index * step,
            y + pad + (height - 2 * pad) * (1 - (value - lo) / span),
        )
        for index, value in enumerate(values)
    ]
    stroke = HexColor(colour)
    area = writer.beginPath()
    area.moveTo(points[0][0], y + pad)
    for point in points:
        area.lineTo(point[0], point[1])
    area.lineTo(points[-1][0], y + pad)
    area.close()
    writer.setFillColor(Color(stroke.red, stroke.green, stroke.blue, 0.13))
    writer.drawPath(area, stroke=0, fill=1)
    line = writer.beginPath()
    line.moveTo(*points[0])
    for point in points[1:]:
        line.lineTo(point[0], point[1])
    writer.setStrokeColor(stroke)
    writer.setLineWidth(1.6)
    writer.drawPath(line, stroke=1, fill=0)


def _draw_kpi_tile(
    writer: pdf_canvas.Canvas, tile: KpiTile, x: float, y: float, width: float
) -> None:
    """Draw one KPI card at ``x``/``y``: grade, hero figure, stats, sparkline.

    The interior follows the HTML tile's order (language + badge, article
    title, hero figure with its arrow, the label under it, two supporting
    counts, sparkline), so a reader who knows one artifact recognises the
    other. Coverage gaps get a dashed accent bar and no stats -- a missing
    article must not look like a measured one.
    """
    height = KPI_TILE_H
    top = y + height
    writer.setFillColor(WHITE)
    writer.rect(x, y, width, height, stroke=0, fill=1)
    writer.setStrokeColor(RULE)
    writer.setLineWidth(1.0)
    writer.rect(x, y, width, height, stroke=1, fill=0)
    if tile["gap"]:
        writer.saveState()
        writer.setDash(3, 2)
        writer.setFillColor(GAP_FG)
        writer.rect(x, top - 3, width, 3, stroke=0, fill=1)
        writer.restoreState()
    else:
        writer.setFillColor(HexColor(tile["colour"]))
        writer.rect(x, top - 3, width, 3, stroke=0, fill=1)

    pad = 8.0
    inner = width - 2 * pad
    writer.setFont(FONT_BOLD, 8)
    writer.setFillColor(GAP_FG if tile["gap"] else HexColor(tile["colour"]))
    writer.drawString(x + pad, top - 16, pdf_glyphs(tile["lang"]))
    _draw_pill(writer, tile["grade"], tile["pill"], x + pad, inner, top - 23, 13)
    writer.setFont(FONT, 7)
    writer.setFillColor(MUTED)
    writer.drawString(x + pad, top - 30, fit(tile["title"], FONT, 7, inner))

    hero_colour = {"up": UP_FG, "down": DOWN_FG}.get(tile["kind"], FLAT_FG)
    if tile["gap"]:
        hero_colour = GAP_FG
    writer.setFont(FONT_BOLD, 16)
    writer.setFillColor(hero_colour)
    writer.drawString(
        x + pad, top - 52, pdf_glyphs(f"{tile['arrow']}{tile['hero']}")
    )
    writer.setFont(FONT_BOLD, 6.5)
    writer.setFillColor(MUTED)
    writer.drawString(x + pad, top - 63, tile["hero_label"].upper())

    if tile["stats"]:
        half = inner / 2
        for index, (value, label) in enumerate(tile["stats"]):
            cell_x = x + pad + index * half
            writer.setFont(FONT_BOLD, 9.5)
            writer.setFillColor(INK)
            writer.drawString(cell_x, top - 78, value)
            writer.setFont(FONT_BOLD, 6.5)
            writer.setFillColor(MUTED)
            writer.drawString(
                cell_x, top - 87, fit(label.upper(), FONT_BOLD, 6.5, half - 6)
            )
        _draw_sparkline(
            writer, tile["sparkline"], tile["colour"], x + pad, top - 112, inner, 17
        )


def render_pdf(
    analysis: AnalysisPayload,
    chart_png: Path | None,
    out_path: Path,
    translator: i18n.Translator | None = None,
    *,
    table_key: Mapping[str, str] | None = None,
) -> int:
    """Write the PDF, carrying every block the HTML report carries.

    The layout cursor (``_Sheet``) closes the page and opens a fresh one
    whenever the next block would cross the bottom margin, so a long report
    simply runs onto page two -- nothing is dropped to make it fit, and the
    attribution footer with its page number is printed on every sheet.

    Args:
        analysis: The ``analysis.json`` payload.
        chart_png: Chart to draw scaled; ignored when missing/unreadable.
        out_path: Output file; the caller decides what to do on failure.
        translator: Target language; English when omitted.
        table_key: The manifest's ``table_key`` block -- the agent-written
            decoding printed under the table. Absent means the English
            defaults, recorded as untranslated in a localised report.

    Returns:
        The number of pages written (always at least 1).

    Raises:
        ReportOverflow: When a block cannot fit even on an empty page; no
            complete PDF is produced in that case.
    """
    tr = translator or i18n.english()
    writer = pdf_canvas.Canvas(str(out_path), pagesize=letter)
    title = tr.t("report.title", topic=str(analysis["topic"]))
    writer.setTitle(title)
    writer.setAuthor("wikipedia-interest-analyzer")

    footer_text = footer_for(analysis, tr)

    def draw_footer(page_number: int) -> None:
        """Attribution plus page number, printed in the foot of every page."""
        writer.setStrokeColor(RULE)
        writer.setLineWidth(0.5)
        writer.line(MARGIN, MARGIN - 12, PAGE_W - MARGIN, MARGIN - 12)
        writer.setFont(FONT, 6.8)
        writer.setFillColor(MUTED)
        writer.drawString(MARGIN, MARGIN - 22, footer_text)
        writer.drawRightString(PAGE_W - MARGIN, MARGIN - 22, str(page_number))

    sheet = _Sheet(writer, draw_footer)

    # --- masthead: accent band carrying kicker, title and subtitle ----------
    # Same three blocks the HTML header prints, in the same order; the band
    # is decoration, so the vertical budget grows only by its padding.
    kicker = tr.t("report.kicker").upper()
    subtitle = subtitle_for(analysis, tr)
    kicker_lines = wrap(kicker, FONT_BOLD, 7.5, CONTENT_W - 20)
    title_lines = wrap(title, FONT_BOLD, 17, CONTENT_W - 20)
    subtitle_lines = wrap(subtitle, FONT, 8, CONTENT_W - 20)
    band_height = (
        8
        + 11 * len(kicker_lines)
        + 5
        + 21 * len(title_lines)
        + 11 * len(subtitle_lines)
        + 8
    )
    sheet.need(band_height + 10, "header band")
    band_top = sheet.y + 6
    writer.setFillColor(ACCENT)
    band_bottom = band_top - band_height
    writer.rect(MARGIN, band_bottom, CONTENT_W, band_height, stroke=0, fill=1)
    cursor = band_top - 8
    writer.setFont(FONT_BOLD, 7.5)
    for line in kicker_lines:
        cursor -= 11
        writer.setFillColor(GOLD)
        writer.drawString(MARGIN + 10, cursor, line)
    cursor -= 5
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
    writer.rect(MARGIN, band_bottom, CONTENT_W, 2, stroke=0, fill=1)
    sheet.y = band_bottom - 10

    # --- verdict: dark panel, gold rule and tag ----------------------------
    # The HTML appends the coverage-gap sentence to the headline; the PDF
    # prints the very same suffix so both artifacts read identically.
    gap_suffix = gaps_note_text(analysis, tr)
    verdict = headline_for(analysis, tr)
    if gap_suffix:
        verdict = f"{verdict} {gap_suffix}"
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

    # --- KPI tiles ----------------------------------------------------------
    # The cards the HTML grid shows, one row at a time: a long list of
    # languages flows onto the next page instead of being squeezed or cut.
    tiles = kpi_tiles_data(analysis, tr)
    if tiles:
        cols = _kpi_columns(len(tiles))
        tile_w = (CONTENT_W - KPI_GAP * (cols - 1)) / cols
        row_top = sheet.y
        for index, tile in enumerate(tiles):
            column = index % cols
            if column == 0:
                sheet.need(KPI_TILE_H, "kpi tiles")
                row_top = sheet.y
            _draw_kpi_tile(
                writer,
                tile,
                MARGIN + column * (tile_w + KPI_GAP),
                row_top - KPI_TILE_H,
                tile_w,
            )
            if column == cols - 1 or index == len(tiles) - 1:
                sheet.y = row_top - KPI_TILE_H - KPI_GAP

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
    # The column header repeats on every page the table spans: a row that
    # starts a fresh sheet still has to say what its numbers mean.
    columns = table_columns_for(analysis)
    rows = table_rows_for(analysis, tr, columns=columns)
    row_height = 12.5

    x_positions = []
    cursor_x = MARGIN
    for _, width in columns:
        x_positions.append(cursor_x)
        cursor_x += width

    def draw_table_header() -> None:
        """Paint the header row, breaking a page first if it needs the room."""
        sheet.need(16 + row_height, "comparison table header")
        top = sheet.y
        writer.setFillColor(ACCENT)
        writer.rect(MARGIN, top - 16, CONTENT_W, 16, stroke=0, fill=1)
        writer.setFont(FONT_BOLD, 7.8)
        writer.setFillColor(WHITE)
        for (name, width), x in zip(columns, x_positions, strict=True):
            # A translated header can be longer than its fixed column: shorten it
            # rather than let it collide with the neighbour it shares a row with.
            value = fit(name, FONT_BOLD, 7.8, width - 8)
            if name in ALIGN_LEFT:
                writer.drawString(x + 4, top - 11.5, value)
            else:
                writer.drawRightString(x + width - 4, top - 11.5, value)
        sheet.y = top - 16

    draw_table_header()

    for index, row in enumerate(rows):
        if sheet.y - row_height < BOTTOM:
            draw_table_header()
        sheet.y -= row_height
        if row["gap"]:
            writer.setFillColor(GAP_ROW_BG)
            writer.rect(MARGIN, sheet.y, CONTENT_W, row_height, stroke=0, fill=1)
        elif index % 2:
            writer.setFillColor(ZEBRA)
            writer.rect(MARGIN, sheet.y, CONTENT_W, row_height, stroke=0, fill=1)
        for (name, width), x in zip(columns, x_positions, strict=True):
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
    # in view rather than down among the caveats. The ranking sentence leads,
    # because it explains the order of the rows still on screen.
    ranked_text = ranked_by_text(analysis, tr)
    if ranked_text:
        sheet.gap(2)
        sheet.text(ranked_text, FONT, 7.2, 9.4, color=MUTED, what="ranking note")
    key_items = table_key_items(table_key, tr, columns=columns)
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

    # --- the user's own success criteria ------------------------------------
    # The same block the HTML prints, in the same place: heading, one line
    # per rule carrying every language's mark, then the n/m summaries. Both
    # come from criteria_view(), so the two artifacts cannot grade a rule
    # differently.
    criteria = criteria_view(analysis, tr)
    if criteria:
        criteria_heading, rule_lines, criteria_summary = criteria
        sheet.gap(4)
        sheet.need(14, "criteria heading")
        writer.setFillColor(GOLD)
        writer.rect(MARGIN, sheet.y - 10, 3, 10, stroke=0, fill=1)
        sheet.text(
            criteria_heading.upper(),
            FONT_BOLD,
            8.5,
            12,
            color=ACCENT,
            x=MARGIN + 9,
            max_width=CONTENT_W - 9,
            what="criteria heading",
        )
        for rule in rule_lines:
            sheet.text(
                "\u2022  " + rule,
                FONT,
                7.4,
                9.6,
                indent=10,
                max_width=CONTENT_W,
                what="criteria rule",
            )
        sheet.text(
            criteria_summary,
            FONT,
            7.4,
            9.6,
            indent=10,
            max_width=CONTENT_W,
            color=MUTED,
            what="criteria summary",
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
    # Pages closed by a page break already had theirs drawn (by _Sheet); this
    # is the last one, and then the document is complete.
    draw_footer(sheet.pages)

    writer.showPage()
    writer.save()
    return sheet.pages


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
    """Write the HTML report and a PDF carrying the same content.

    The PDF is paginated, so length is no longer a failure -- ``1`` is
    reserved for the one case printing cannot solve: a block that would not
    fit on an empty page.

    Returns:
        ``0`` on success; ``1`` when the PDF could not be printed (the HTML
        is still written and the partial PDF is deleted).
    """
    common.configure_console()
    args = build_parser().parse_args(argv)
    analysis = common.read_json(args.analysis)
    chart = Path(args.chart) if args.chart else None

    # The decoding under the table comes from the manifest, never from the
    # translation catalogue: it is generated once per study, not translated.
    table_key = load_table_key(Path(args.study))
    warn_unknown_table_key(table_key, columns=table_columns_for(analysis))

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
        pages = render_pdf(analysis, chart, pdf_path, translator, table_key=table_key)
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
        print(f"wrote {pdf_path} ({pages} page{'s' if pages != 1 else ''})")

    # Printed on both paths: an untranslated report must be flagged even when
    # the PDF failed for an unrelated reason.
    i18n.warn_untranslated(translator)
    return status


if __name__ == "__main__":
    raise SystemExit(main())
