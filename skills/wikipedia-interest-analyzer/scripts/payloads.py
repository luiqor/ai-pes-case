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
    overrides: NotRequired[dict[str, str]]
    resolution: NotRequired[Resolution | None]
    report_language: NotRequired[str]


# --------------------------------------------------------------------------
# series.json
# --------------------------------------------------------------------------
class WindowRange(TypedDict):
    """A resolved window: both ends complete months plus the month count."""

    since: str
    until: str
    months: int


class RequestParameters(TypedDict):
    """The pageview query parameters actually used, echoed into every output."""

    access: str
    agent: str
    granularity: str


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


class ShareRanking(TypedDict):
    """One row of the by-share ranking."""

    language: str
    share_ppm: float


class GrowthRanking(TypedDict):
    """One row of the by-growth ranking; either percentage can be missing."""

    language: str
    share_pct: float | None
    article_pct: float | None


class Comparison(TypedDict):
    """Cross-language rankings; see SKILL.md "Answering the user's question"."""

    by_share_ppm: list[ShareRanking]
    by_growth_share_pct: list[GrowthRanking]
    highest_share: str | None
    fastest_growth: str | None


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

# Path enums for the pageviews API, from references/api.md (verified).
ACCESS_VALUES = ("all-access", "desktop", "mobile-app", "mobile-web")
AGENT_VALUES = ("all-agents", "user", "spider", "automated")


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


class StudyManifestModel(BaseModel):
    """Runtime shape of ``study.json``; fields mirror :class:`StudyManifest`."""

    topic: str = Field(min_length=1)
    languages: list[Annotated[str, Field(pattern=LANG_PATTERN)]] = Field(min_length=1)
    version: int = 1
    window: StudyWindowModel = Field(default_factory=StudyWindowModel)
    access: Literal["all-access", "desktop", "mobile-app", "mobile-web"] = "all-access"
    agent: Literal["all-agents", "user", "spider", "automated"] = "user"
    granularity: Literal["monthly"] = "monthly"
    overrides: dict[Annotated[str, Field(pattern=LANG_PATTERN)], str] = {}
    resolution: ResolutionModel | None = None
    report_language: Annotated[str, Field(pattern=LANG_PATTERN)] | None = None


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
