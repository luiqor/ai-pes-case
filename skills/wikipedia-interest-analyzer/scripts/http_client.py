"""The one place that owns Wikimedia HTTP transport state.

The session, the request cadence, retry/backoff and the response cache all
live on a :class:`WikiClient` instance instead of module globals, so:

* nothing about HTTP is shared implicitly between imports (no lazy
  ``_get_session()`` global, no process-wide cache switch),
* a composition root (each ``main``) builds one client and passes it down,
  so politeness state spans every request of a run,
* tests inject a fixture-backed client instead of patching a module
  attribute -- ``tests/conftest.py`` wires that at this factory seam.

URL building (``api.py``), error classification (``errors.py``) and the cache
*directory* policy stay out of this module -- all still reachable as
``common.<name>``; this module only performs requests.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import sys
import time
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

import common
import requests


def _build_session() -> requests.Session:
    """New session carrying the required ``User-Agent`` (access policy)."""
    session = requests.Session()
    session.headers.update(
        {"User-Agent": common.USER_AGENT, "Accept": "application/json"}
    )
    return session


def _safe_detail(response: requests.Response) -> Any:
    """Best-effort diagnostic from a failed response (body or ``detail``)."""
    try:
        payload = response.json()
    except ValueError:
        return response.text[:500]
    if isinstance(payload, dict):
        return payload.get("detail", payload)
    return payload


# Distinguishes "no usable cache entry" from a cached body that is null.
_CACHE_MISS = object()


class WikiClient(common.JsonFetcher):
    """GET JSON from Wikimedia: politeness, retries, and the disk cache.

    One instance owns all mutable transport state. It is safe to call
    ``get_json`` repeatedly; the request cadence is tracked per instance,
    which is exactly why a run constructs *one* client and threads it
    through every stage.

    Args:
        cache_dir: Where responses are cached; ``None`` means
            ``common.CACHE_DIR`` (``<skill>/cache`` or ``WIA_CACHE_DIR``).
        cache_enabled: When false, entries are neither read nor written
            (``--no-cache``); existing files are left untouched.
        session: Pre-built session (tests inject a fake); ``None`` builds
            one with the configured ``User-Agent``.
        sleep: Wait primitive (tests inject a recorder); defaults to
            ``time.sleep``. Used for both the polite delay and backoff.
    """

    def __init__(
        self,
        *,
        cache_dir: Path | None = None,
        cache_enabled: bool = True,
        session: requests.Session | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """Configure the transport; every argument is documented on the class."""
        self._cache_dir = cache_dir if cache_dir is not None else common.CACHE_DIR
        self._cache_enabled = cache_enabled
        self._session = session if session is not None else _build_session()
        self._sleep = sleep
        self._last_request_at = 0.0

    # -- lifecycle ---------------------------------------------------------
    def __enter__(self) -> WikiClient:
        """Enter a ``with`` block that owns this client."""
        return self

    def __exit__(self, *exc_info: object) -> None:
        """Close the session (see :meth:`close`)."""
        self.close()

    def close(self) -> None:
        """Release the underlying HTTP session; do not use the client after."""
        self._session.close()

    # -- request path ------------------------------------------------------
    def get_json(self, url: str) -> Any:
        """GET ``url`` and return the decoded JSON body.

        Cached responses are returned without touching the network or the
        request cadence, which is what makes iterative follow-up queries
        cheap.

        Args:
            url: Fully built request URL (always from a ``common`` builder).

        Returns:
            The decoded JSON body; shape depends on the endpoint (Wikimedia's).

        Raises:
            ApiError: After ``common.MAX_ATTEMPTS`` for transient failures
                (429/5xx, network), or immediately for deterministic 4xx and
                non-JSON 200 responses. ``kind`` says which case occurred.
        """
        if self._cache_enabled:
            cached_body = self._read_cache(url)
            if cached_body is not _CACHE_MISS:
                return cached_body

        last_error: common.ApiError | None = None
        for attempt in range(common.MAX_ATTEMPTS):
            self._polite_delay()
            try:
                response = self._session.get(url, timeout=30)
            except requests.RequestException as exc:
                last_error = common.ApiError(
                    f"network error requesting {url}: {exc}",
                    url=url,
                    kind="network",
                )
                self._backoff(attempt)
                continue
            finally:
                self._last_request_at = time.monotonic()

            if response.status_code == 200:
                try:
                    body = response.json()
                except ValueError as exc:
                    raise common.ApiError(
                        f"non-JSON response from {url}: {response.text[:200]}",
                        status=200,
                        url=url,
                        kind="bad_body",
                    ) from exc
                if self._cache_enabled:
                    self._write_cache(url, body)
                return body

            detail = _safe_detail(response)
            kind = common._classify(response.status_code, detail)
            error = common.ApiError(
                f"HTTP {response.status_code} from {url}: {detail}",
                status=response.status_code,
                url=url,
                kind=kind,
                detail=detail,
            )
            if response.status_code == 429 or response.status_code >= 500:
                last_error = error
                self._backoff(attempt)  # transient -> retry with backoff
                continue
            raise error  # deterministic 4xx -> retrying would not help

        raise last_error or common.ApiError(
            f"request failed after {common.MAX_ATTEMPTS} attempts: {url}"
        )

    # -- politeness --------------------------------------------------------
    def _polite_delay(self) -> None:
        """Wait out the gap since this instance's previous request."""
        if not self._last_request_at:
            return
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < common.REQUEST_DELAY_SECONDS:
            self._sleep(common.REQUEST_DELAY_SECONDS - elapsed)

    def _backoff(self, attempt: int) -> None:
        self._sleep(
            common.BACKOFF_SECONDS[min(attempt, len(common.BACKOFF_SECONDS) - 1)]
        )

    # -- cache -------------------------------------------------------------
    def _cache_path(self, url: str) -> Path:
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
        return self._cache_dir / f"{digest}.json"

    def _write_cache(self, url: str, body: Any) -> None:
        try:
            self._cache_dir.mkdir(parents=True, exist_ok=True)
            record = {"url": url, "fetched_at": date.today().isoformat(), "body": body}
            self._cache_path(url).write_text(
                json.dumps(record, ensure_ascii=False), encoding="utf-8"
            )
        except OSError as exc:
            # A read-only or full disk must never break the analysis, but it must
            # not be invisible either: silently uncacheable runs look like a
            # permanent cold start on the next query.
            print(
                f"warning: could not write the response cache ({exc}); "
                "later runs will refetch everything",
                file=sys.stderr,
            )

    def _read_cache(self, url: str) -> Any:
        """Return the cached body for ``url``, or ``_CACHE_MISS`` when none is usable.

        A missing entry is the normal cold-start case and stays silent. An
        *unreadable* entry is different: data is being discarded, so the reason
        is reported on stderr before the entry is deleted (best effort -- a cache we
        cannot rewrite must not abort the run either).
        """
        path = self._cache_path(url)
        if not path.is_file():
            return _CACHE_MISS
        try:
            return json.loads(path.read_text(encoding="utf-8"))["body"]
        except (json.JSONDecodeError, KeyError, OSError) as exc:
            print(
                f"warning: discarding unreadable cache entry {path.name} "
                f"({type(exc).__name__}); refetching",
                file=sys.stderr,
            )
            with contextlib.suppress(OSError):
                path.unlink()
            return _CACHE_MISS


def default_client(*, cache_enabled: bool = True) -> WikiClient:
    """Build the transport a composition root uses for one run.

    The indirection is the test seam: ``tests/conftest.py`` replaces *this
    factory* (not the class, not the callers) with one returning an offline
    fixture-backed client, so every default-transport code path becomes
    network-free at once -- while tests that need a real ``WikiClient``
    (cache behaviour, retry logic) can still construct one directly.
    """
    return WikiClient(cache_enabled=cache_enabled)
