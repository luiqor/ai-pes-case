"""Shared settings, utilities, and the façade over the leaf modules.

What lives *here*:

* settings resolved once at import: ``SKILL_ROOT``, the **required**
  ``User-Agent`` header, the cache directory, the transport constants;
* leaf utilities every stage prints or writes with: :func:`clip`,
  :func:`strip_html`, :func:`configure_console`, :func:`read_json` /
  :func:`write_json`, :func:`clear_cache`.

What lives elsewhere and is re-exported here (so every existing
``common.<name>`` reference -- SKILL.md rule 1, ``references/edge-cases.md``,
every script -- keeps working unchanged):

* :mod:`errors` -- :class:`ApiError`, the :class:`~errors.JsonFetcher`
  transport protocol, and the status/body classification;
* :mod:`api` -- the **only** implementations of pageviews / Action API /
  Wikidata URL building plus the two API entry points;
* :mod:`months` -- ``YYYY-MM`` validation, shifts, and windows.

How to *perform* requests -- session, polite cadence, retry/backoff, response
cache -- lives in :mod:`http_client`: inject an
:class:`~errors.JsonFetcher` (production: ``http_client.WikiClient()``)
instead of reaching for module state.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

from api import (
    ACTION_API,
    DEFAULT_ACCESS,
    DEFAULT_AGENT,
    DEFAULT_GRANULARITY,
    REST_BASE,
    WIKIDATA_API,
    action_api,
    aggregate_url,
    encode_title,
    iter_pages,
    page_exists,
    per_article_url,
    project_for,
    wikidata_api,
)
from errors import ApiError, JsonFetcher
from errors import _classify as _classify
from months import (
    DATA_START_MONTH,
    clamp_to_data_start,
    default_window,
    last_complete_month,
    month_range,
    month_to_stamp,
    parse_month,
    shift_month,
)

__all__ = [
    # settings
    "SKILL_ROOT",
    "CACHE_DIR",
    "USER_AGENT",
    "REQUEST_DELAY_SECONDS",
    "MAX_ATTEMPTS",
    "BACKOFF_SECONDS",
    # leaf utilities
    "clear_cache",
    "clip",
    "strip_html",
    "configure_console",
    "read_json",
    "write_json",
    # from errors
    "ApiError",
    "JsonFetcher",
    # from api
    "REST_BASE",
    "ACTION_API",
    "WIKIDATA_API",
    "DEFAULT_ACCESS",
    "DEFAULT_AGENT",
    "DEFAULT_GRANULARITY",
    "encode_title",
    "project_for",
    "per_article_url",
    "aggregate_url",
    "action_api",
    "wikidata_api",
    "iter_pages",
    "page_exists",
    # from months
    "DATA_START_MONTH",
    "parse_month",
    "month_to_stamp",
    "month_range",
    "shift_month",
    "last_complete_month",
    "default_window",
    "clamp_to_data_start",
]

SKILL_ROOT = Path(__file__).resolve().parent.parent


def _cache_dir_from_env() -> Path:
    """Resolve ``WIA_CACHE_DIR`` (empty counts as unset) to the cache folder.

    Returns:
        The configured directory, or ``<skill>/cache`` when unset/empty --
        exactly the path every reader and writer in this module uses.

    Raises:
        SystemExit: The configured path exists but is not a directory. Every
            cache write would otherwise fail for the whole run; failing here
            says so once, at startup, instead of warning per request.
    """
    raw = os.environ.get("WIA_CACHE_DIR")
    path = Path(raw) if raw and raw.strip() else SKILL_ROOT / "cache"
    if path.exists() and not path.is_dir():
        raise SystemExit(f"error: WIA_CACHE_DIR {path} exists but is not a directory")
    return path


CACHE_DIR = _cache_dir_from_env()

# The numeric rate limit was deliberately never measured. Stay conservative:
# one request at a time with a fixed gap, and back off hard on 429/5xx.
REQUEST_DELAY_SECONDS = 0.5
MAX_ATTEMPTS = 4
BACKOFF_SECONDS = (1.0, 2.0, 4.0)


def _user_agent_from_env() -> str:
    """Resolve ``WIA_USER_AGENT`` (the HTTP ``User-Agent`` header).

    Wikimedia's access policy requires a descriptive User-Agent, and
    ``requests`` rejects header values containing CR/LF outright, so both
    problems are caught here -- once, at startup -- rather than surfacing
    as a per-request failure.

    Returns:
        The configured string verbatim (never rewritten), or the default
        contact string when the variable is unset.

    Raises:
        SystemExit: The variable is set but empty, or contains a line break.
    """
    raw = os.environ.get("WIA_USER_AGENT")
    if raw is None:
        return "wikipedia-interest-analyzer/0.1 (set WIA_USER_AGENT to your contact)"
    if not raw.strip():
        raise SystemExit(
            "error: WIA_USER_AGENT is set but empty; "
            "unset it or provide a contact string"
        )
    if "\n" in raw or "\r" in raw:
        raise SystemExit("error: WIA_USER_AGENT must not contain line breaks")
    return raw


USER_AGENT = _user_agent_from_env()

_TAG_RE = re.compile(r"<[^>]+>")


# --------------------------------------------------------------------------
# Cache
# --------------------------------------------------------------------------
def clear_cache() -> int:
    """Delete every cached response. Returns the number of files removed."""
    if not CACHE_DIR.is_dir():
        return 0
    removed = 0
    for path in CACHE_DIR.glob("*.json"):
        path.unlink(missing_ok=True)
        removed += 1
    return removed


# --------------------------------------------------------------------------
# Small utilities
# --------------------------------------------------------------------------
def clip(text: str, limit: int = 80) -> str:
    """Shorten ``text`` to about ``limit`` characters, cutting on a word boundary.

    ``text[:80]`` sliced search descriptions mid-word (``... Diese Pro)``), which
    reads like corruption rather than truncation. Falls back to a hard cut when
    there is no space to break on.
    """
    if len(text) <= limit:
        return text
    cut = text.rfind(" ", 0, limit)
    if cut <= 0:
        cut = limit
    return text[:cut].rstrip(" ,;:.") + "..."


def strip_html(text: str) -> str:
    """Remove HTML tags from a search snippet and collapse the whitespace."""
    return re.sub(r"\s+", " ", _TAG_RE.sub("", text or "")).strip()


def configure_console() -> None:
    """Emit UTF-8 on stdout/stderr regardless of the host's locale.

    Article titles are routinely non-ASCII (``Přerušovaný půst``,
    ``Wielki post``), and the Windows console defaults to a legacy code page
    such as cp1252, which raises ``UnicodeEncodeError`` while printing them.
    Call this at the start of every CLI entry point.

    Tolerated failures (they never abort the run, and each is reported when
    the stream still accepts writes): the stream has no ``reconfigure``
    attribute, the stream is already closed, or the OS refuses the change
    (``OSError`` / ``ValueError``).
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError) as exc:
            # The stream itself is what failed, so this warning may fail too;
            # that is the documented end of the line, not a hidden swallow.
            with contextlib.suppress(OSError, ValueError):
                print(
                    f"warning: could not switch {stream} to UTF-8 ({exc}); "
                    "non-ASCII article titles may print incorrectly",
                    file=sys.stderr,
                )


def read_json(path: str | Path) -> Any:
    """Read a JSON file and return its decoded content.

    Generic reader: shape validation is the caller's job and happens in the
    ``payloads`` module at each trust boundary (see ``validation-pydantic``).

    Raises:
        OSError: If the file cannot be read.
        json.JSONDecodeError: If the file is not valid JSON.
    """
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path: str | Path, payload: Any) -> None:
    """Write ``payload`` as pretty-printed UTF-8 JSON, creating parent dirs.

    ``ensure_ascii=False`` keeps titles like ``Přerušovaný půst`` readable in
    the artifact; the trailing newline keeps diffs and some tools happy.

    Raises:
        OSError: If the file cannot be written.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
