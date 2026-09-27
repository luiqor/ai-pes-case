"""Declared shapes of the artifacts that flow between pipeline stages.

``study.json`` -> ``resolve`` -> ``series.json`` -> ``analysis.json`` -> chart/report.

Two layers, split by who can be trusted:

* **TypedDict** (compile-time) -- checked by mypy where payloads are
  *constructed*: the dict literals the stages build, replacing
  ``dict[str, Any]`` in public signatures. No runtime cost, and subscript
  access keeps working exactly as before.
* **Pydantic** (runtime) -- applied where *untrusted* input enters:
  ``study.json`` (the file the skill's contract lets users hand-edit) and raw
  pageviews response points, so a hand-edited, truncated or corrupted file
  fails with a field path instead of a ``KeyError`` deep inside a stage.
  The tool-written artifacts (``series.json``/``analysis.json``) stay on the
  compile-time layer only -- their producer is this codebase itself.

``tests/test_payloads.py`` pins the Pydantic fields to the TypedDict field
set, so the two layers cannot drift apart.

.. _TypedDict: https://docs.python.org/3/library/typing.html#typing.TypedDict
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any, Literal, NotRequired, TypedDict, cast

# Path enums for the pageviews API (references/api.md): defined next to the
# URL builders in ``api.py`` and re-exported here, so the manifest and the
# paths cannot disagree about the same enum.
from api import ACCESS_VALUES, AGENT_VALUES  # noqa: F401  (re-export)
from pydantic import BaseModel, Field, ValidationError


# --------------------------------------------------------------------------
# study.json
# --------------------------------------------------------------------------
class StudyWindow(TypedDict, total=False):
    """Requested window; both keys optional -- absent means "default window"."""

    since: str
    until: str


class Resolution(TypedDict, total=False):
    """Output of the resolve stage, stored inside the manifest for review."""

    topic: str
    qid: str
    label: str
    description: str | None
    search_language: str
    languages: list[str]
    articles: dict[str, dict[str, Any] | None]
    gaps: list[str]
    candidates: dict[str, list[dict[str, Any]]]
    search_hits: list[dict[str, Any]]
    warnings: list[str]
    labels: dict[str, str]


class CriteriaSpec(TypedDict, total=False):
    """``criteria``: how the user wants the audiences ranked in the report.

    Only ``rank_by`` is read today; extra keys a hand-edited manifest carries
    survive validation untouched (see :func:`validate_study`).
    """

    rank_by: str


class StudyManifest(TypedDict):
    """``study.json`` -- the user-editable manifest.

    Only ``topic`` and ``languages`` are required; every other key is
    optional because existing manifests predate it or the user deleted it,
    and the stages supply documented defaults for those cases.
    """

    topic: str
    languages: list[str]
    version: NotRequired[int]
    window: NotRequired[StudyWindow]
    access: NotRequired[str]
    agent: NotRequired[str]
    granularity: NotRequired[str]
    #: Optional data layers fetched on top of the two base series; each adds
    #: requests only while it is listed (see ``fetch.fetch_series``).
    layers: NotRequired[list[str]]
    #: The user's ranking criterion; drives ``comparison.ranked_by``.
    criteria: NotRequired[CriteriaSpec]
    overrides: NotRequired[dict[str, str]]
    resolution: NotRequired[Resolution | None]
    report_language: NotRequired[str]
    #: Agent-written decoding of the table's abbreviated headers (column ->
    #: definition, plus an optional "heading"). Not part of the translation
    #: catalogue: it is generated, never translated. See ``report.table_key_items``.
    table_key: NotRequired[dict[str, str]]


# --------------------------------------------------------------------------
# series.json
# --------------------------------------------------------------------------
class WindowRange(TypedDict):
    """A resolved window: both ends complete months plus the month count."""

    since: str
    until: str
    months: int


class RequestParameters(TypedDict):
    """The pageview query parameters actually used, echoed into every output.

    Also carries the analysis *selection* (``layers``, ``rank_by``) so the
    later stages can read them from ``series.json`` alone, without the
    manifest being open.
    """

    access: str
    agent: str
    granularity: str
    layers: NotRequired[list[str]]
    rank_by: NotRequired[str]


class TopRank(TypedDict):
    """Where the article placed in the project's monthly top list.

    ``rank`` is ``None`` when the article is not among the listed entries --
    "outside the top" is a measurement result, not a missing one.
    """

    month: str
    rank: int | None
    list_size: int


class LanguageSeries(TypedDict):
    """One language edition's aligned monthly series (see fetch.py)."""

    language: str
    project: str
    article_title: str
    resolved_via: str
    native_gap: bool
    labels: list[str]
    article_views: list[int]
    project_views: list[int]
    #: access layer: monthly article views per access channel, aligned to
    #: ``labels`` (absent unless the ``access`` layer was requested).
    access_views: NotRequired[dict[str, list[int]]]
    #: bot layer: monthly article views with ``agent=all-agents``, aligned to
    #: ``labels`` (absent unless the ``bot`` layer was requested).
    all_agents_views: NotRequired[list[int]]
    #: top layer: rank in the project's top list for the window's last month
    #: (absent unless the ``top`` layer was requested).
    top_rank: NotRequired[TopRank]


class SeriesPayload(TypedDict):
    """``series.json`` -- aligned raw data, one entry per measured language."""

    generated_at: str
    topic: str
    qid: str | None
    window: WindowRange
    parameters: RequestParameters
    series: dict[str, LanguageSeries]
    gaps: list[str]
    warnings: list[str]


# --------------------------------------------------------------------------
# analysis.json
# --------------------------------------------------------------------------
class WindowSplit(TypedDict, total=False):
    """Result of splitting the window into comparable halves (analyze.py)."""

    available: bool
    reason: str
    months_each: int
    first_labels: list[str]
    second_labels: list[str]
    first_range: str
    second_range: str
    aligned: bool
    dropped_months: list[str]


class YoyMetrics(TypedDict, total=False):
    """Second-half-vs-first-half comparison; only filled when it is available."""

    available: bool
    months_each: int
    first_range: str
    second_range: str
    aligned: bool
    dropped_months: list[str]
    article_first: int
    article_second: int
    article_pct: float | None
    project_first: int
    project_second: int
    project_pct: float | None
    share_first_ppm: float
    share_second_ppm: float
    share_pct: float | None


class TrendFit(TypedDict):
    """Ordinary least-squares fit of a series against the month index."""

    slope_per_month: float
    intercept: float
    r2: float


class SeasonalityProfile(TypedDict, total=False):
    """Per-calendar-month means plus the peak dominance ratio.

    Present in two shapes: the unavailable shape (``available`` false and a
    ``reason``) and the measured shape (all the statistics).
    """

    available: bool
    reliable: bool
    reason: str
    cycles: int
    peak_month: int
    peak_mean: float
    trough_month: int
    trough_mean: float
    overall_mean: float
    peak_ratio: float
    profile: dict[int, float]


class MetricFlags(TypedDict):
    """Boolean signals consumed by grading and by the report."""

    direction_agrees: bool
    seasonality_observed: bool
    strong_seasonality: bool
    small_volume: bool


class SeriesView(TypedDict):
    """Per-language series copied into the analysis so charts/report need one file."""

    labels: list[str]
    article_views: list[int]
    project_views: list[int]
    share_ppm: list[float]


class AccessSplit(TypedDict):
    """Share of the window's article views per access channel, in percent.

    Present only when the ``access`` layer was fetched; the four percentages
    sum to 100 (``mobile_pct`` = mobile-web + mobile-app).
    """

    total_views: int
    desktop_pct: float
    mobile_web_pct: float
    mobile_app_pct: float
    mobile_pct: float


class LanguageMetrics(TypedDict):
    """Everything the report says about one language edition."""

    language: str
    article_title: str
    project: str
    resolved_via: str | None
    native_gap: bool
    months: int
    article_total: int
    project_total: int
    share_ppm: float
    mean_monthly_views: float
    yoy: YoyMetrics
    trend: dict[str, TrendFit]
    seasonality: SeasonalityProfile
    flags: MetricFlags
    confidence: str
    confidence_reasons: list[str]
    series: NotRequired[SeriesView]
    #: access layer -- absent unless that layer was fetched.
    access_split: NotRequired[AccessSplit]
    #: bot layer: share of ``all-agents`` views that were *not* ``user``.
    bot_share_pct: NotRequired[float]
    #: top layer: placement in the project's monthly top list.
    top_rank: NotRequired[TopRank]


class ShareRanking(TypedDict):
    """One row of the by-share ranking."""

    language: str
    share_ppm: float


class GrowthRanking(TypedDict):
    """One row of the by-growth ranking; either percentage can be missing."""

    language: str
    share_pct: float | None
    article_pct: float | None


class RankedBy(TypedDict):
    """The user's ranking criterion, resolved into an explicit order.

    ``order`` lists every *measured* language best-first; languages whose
    metric could not be computed (a coverage gap, a layer that was not
    fetched) are simply absent rather than ranked last.
    """

    criterion: str
    order: list[str]


class Comparison(TypedDict):
    """Cross-language rankings; see SKILL.md "Answering the user's question"."""

    by_share_ppm: list[ShareRanking]
    by_growth_share_pct: list[GrowthRanking]
    highest_share: str | None
    fastest_growth: str | None
    #: Present only when the manifest carries ``criteria.rank_by``.
    ranked_by: NotRequired[RankedBy]


class MessageRef(TypedDict, total=False):
    """A translatable sentence: a message id plus its parameters.

    Rendered by :class:`i18n.Translator`, which formats the id's template in
    the requested report language. Three shapes are recognised:

    * ``{"id": ..., "params": {...}}`` -- format one message;
    * ``{"concat": [ref, ...]}`` -- the parts joined with a space (how the
      headline appends its optional sentences);
    * a parameter whose value is a list, or ``{"items": [...], "sep": ...}``,
      is joined with the named join message (``join.comma`` by default).

    ``params`` values are pre-formatted data (numbers, titles, codes) or
    nested refs -- the numbers themselves are never re-translated.
    """

    id: str
    params: dict[str, Any]
    concat: list[MessageRef]


class AnalysisPayload(TypedDict):
    """``analysis.json`` -- metrics, rankings, headline, assumptions, caveats.

    The ``*_i18n`` siblings are the message refs behind the English strings:
    ``headline``/``assumptions``/``limitations`` stay the canonical English
    text (what ``run.py`` prints and what tests assert), while the report and
    chart render the refs in whatever ``--report-lang`` was requested.
    """

    generated_at: str
    topic: str
    qid: str | None
    window: WindowRange
    parameters: RequestParameters
    headline: str
    metrics: dict[str, LanguageMetrics]
    comparison: Comparison
    gaps: list[str]
    warnings: list[str]
    assumptions: list[str]
    limitations: list[str]
    headline_i18n: NotRequired[MessageRef]
    assumptions_i18n: NotRequired[list[MessageRef]]
    limitations_i18n: NotRequired[list[MessageRef]]


# --------------------------------------------------------------------------
# Runtime validators -- the read-boundary half of this module
#
# ``study.json`` is the artifact the skill's contract lets users hand-edit, so
# it is the one file whose contents cannot be trusted. Everything below either
# validates it (``StudyManifestModel`` / ``validate_study`` /
# ``load_study_manifest``) or validates the other untrusted inputs that cross
# the same boundary: pageviews response points (``PageviewPoint``).
# --------------------------------------------------------------------------
LANG_PATTERN = r"^[a-z][a-z0-9-]{0,17}$"
MONTH_PATTERN = r"^\d{4}-(0[1-9]|1[0-2])$"

# Optional data layers (study.json "layers"); each maps to extra requests in
# fetch.py and extra metrics in analyze.py. Verified live 2026-09-27:
# desktop + mobile-web + mobile-app == all-access, and
# user + spider + automated == all-agents, exactly.
LAYER_VALUES = ("access", "bot", "top")

# Ranking criteria (study.json "criteria.rank_by"); the two layer-bound ones
# require their layer, which validate_study checks below.
RANK_BY_VALUES = ("share_ppm", "yoy_share", "mobile_share", "bot_share")
RANK_BY_REQUIRES_LAYER = {"mobile_share": "access", "bot_share": "bot"}


class StudyWindowModel(BaseModel):
    """``window``: both ends optional, each a real ``YYYY-MM`` month."""

    since: str | None = Field(default=None, pattern=MONTH_PATTERN)
    until: str | None = Field(default=None, pattern=MONTH_PATTERN)


class ResolutionModel(BaseModel):
    """``resolution``: every key optional (older manifests), but type-checked.

    Defaults mirror what ``resolve.py`` writes, so a partial or older
    resolution is accepted as long as its *types* are right.
    """

    topic: str | None = None
    qid: str | None = None
    label: str | None = None
    description: str | None = None
    search_language: str | None = None
    languages: list[str] = []
    articles: dict[str, dict[str, Any] | None] = {}
    gaps: list[str] = []
    candidates: dict[str, list[dict[str, Any]]] = {}
    search_hits: list[dict[str, Any]] = []
    warnings: list[str] = []
    labels: dict[str, str] = {}


class CriteriaModel(BaseModel):
    """``criteria``: only ``rank_by`` is interpreted; unknown keys ignored."""

    rank_by: Literal["share_ppm", "yoy_share", "mobile_share", "bot_share"] | None = (
        None
    )


class StudyManifestModel(BaseModel):
    """Runtime shape of ``study.json``; fields mirror :class:`StudyManifest`."""

    topic: str = Field(min_length=1)
    languages: list[Annotated[str, Field(pattern=LANG_PATTERN)]] = Field(min_length=1)
    version: int = 1
    window: StudyWindowModel = Field(default_factory=StudyWindowModel)
    access: Literal["all-access", "desktop", "mobile-app", "mobile-web"] = "all-access"
    agent: Literal["all-agents", "user", "spider", "automated"] = "user"
    granularity: Literal["monthly"] = "monthly"
    layers: list[Literal["access", "bot", "top"]] = []
    criteria: CriteriaModel = Field(default_factory=CriteriaModel)
    overrides: dict[Annotated[str, Field(pattern=LANG_PATTERN)], str] = {}
    resolution: ResolutionModel | None = None
    report_language: Annotated[str, Field(pattern=LANG_PATTERN)] | None = None
    # Agent-written, never translated: column header -> definition, plus an
    # optional "heading". Type-checked only -- the *names* are the report's
    # column keys, which the report itself owns.
    table_key: dict[str, str] = {}


class PageviewPoint(BaseModel):
    """One monthly bucket of a pageviews response (either endpoint).

    ``timestamp`` is the observed ``YYYYMMDDHH`` form for monthly buckets
    (e.g. ``2024100100`` -- see ``references/api.md``); views are counts and
    can never be negative.
    """

    timestamp: str = Field(pattern=r"^\d{10}$")
    views: int = Field(ge=0)


def validate_study(raw: object, source: str | Path) -> StudyManifest:
    """Validate an in-memory ``study.json`` mapping at the trust boundary.

    The *original* mapping is returned rather than a dumped model, so keys
    the tool does not know about (user notes, future fields) survive the next
    save instead of being silently dropped.

    Args:
        raw: The decoded JSON content of the manifest (any type; checked here).
        source: File path or label quoted in the error message.

    Returns:
        ``raw``, validated, as a :class:`StudyManifest`.

    Raises:
        SystemExit: If validation fails; the message lists each bad field by
            location (``window.since: Value should match...``).
    """
    try:
        StudyManifestModel.model_validate(raw)
    except ValidationError as exc:
        detail = "; ".join(
            f"{'.'.join(str(part) for part in err['loc'])}: {err['msg']}"
            for err in exc.errors()
        )
        raise SystemExit(f"error: invalid study manifest {source}: {detail}") from exc
    # Cross-field check the shape alone cannot express: a layer-bound ranking
    # criterion is meaningless without its data layer, and a report that
    # silently fell back to another ranking would be worse than a refusal.
    if isinstance(raw, dict):
        criteria = raw.get("criteria")
        rank_by = criteria.get("rank_by") if isinstance(criteria, dict) else None
        layers = raw.get("layers") or []
        needed = RANK_BY_REQUIRES_LAYER.get(str(rank_by))
        if needed and needed not in layers:
            raise SystemExit(
                f"error: invalid study manifest {source}: criteria.rank_by "
                f"'{rank_by}' requires the '{needed}' data layer -- add "
                f"'\"{needed}\"' to the 'layers' list"
            )
    return cast(StudyManifest, raw)


def load_study_manifest(path: str | Path) -> StudyManifest:
    """Read and validate ``study.json`` -- the one file users hand-edit.

    Args:
        path: Location of the manifest.

    Returns:
        The validated manifest (the original mapping, see
        :func:`validate_study`).

    Raises:
        SystemExit: The manifest is missing (with the ``run.py init``
            remedy), unreadable, not JSON, or fails validation.
    """
    manifest = Path(path)
    if not manifest.is_file():
        raise SystemExit(
            f"error: study manifest not found: {manifest}\n"
            'Create one with:  run.py init --topic "..." --langs pl,cs'
        )
    try:
        raw = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(
            f"error: cannot read study manifest {manifest}: {exc}"
        ) from exc
    return validate_study(raw, manifest)
