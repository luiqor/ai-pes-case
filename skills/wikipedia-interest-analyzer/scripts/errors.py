"""The skill's error vocabulary: classified failures and the transport protocol.

``ApiError`` is the only exception the pipeline raises for API problems, and
its ``kind`` is what lets every caller react precisely (retry, treat as zeros,
fail with a fix hint). Nothing here performs I/O, so it sits at the bottom of
the import graph: :mod:`common`, :mod:`api`, and :mod:`http_client` all build
on it.
"""

from __future__ import annotations

from typing import Any, Protocol


class ApiError(RuntimeError):
    """A failed Wikimedia request, classified so callers can react precisely.

    Args:
        message: Human-readable failure description (also what ``str()`` is).
        status: HTTP status code, when a response was received.
        url: The request URL, so the failure can be reproduced.
        kind: Classification, one of the values listed below.
        detail: Decoded body or exception text, kept for diagnosis.

    ``kind`` is one of:
      * ``invalid_route`` -- the path itself was malformed (a client bug).
      * ``no_data``       -- dates valid, but no data loaded for that range.
      * ``rate_limited``  -- HTTP 429.
      * ``server``        -- HTTP 5xx.
      * ``not_found``     -- HTTP 404 that is neither of the two known bodies.
      * ``network`` / ``bad_body`` -- transport or decoding failure.
      * ``other``         -- anything else.
    """

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        url: str | None = None,
        kind: str = "other",
        detail: Any = None,
    ) -> None:
        """Set the HTTP context (args documented on the class docstring).

        ``str(exc)`` stays the plain, human-readable message.
        """
        super().__init__(message)
        self.status = status
        self.url = url
        self.kind = kind
        self.detail = detail


class JsonFetcher(Protocol):
    """The transport the pipeline needs -- one method, nothing else.

    ``http_client.WikiClient`` is the production implementation (session,
    cadence, retries, cache); tests inject fixture-backed stand-ins.
    Structural typing applies: any object with a compatible ``get_json``
    works, so fakes need no inheritance.
    """

    def get_json(self, url: str) -> Any:
        """GET ``url`` and return the decoded JSON body, or raise :class:`ApiError`."""
        ...


def _classify(status: int, detail: Any) -> str:
    text = str(detail or "")
    if status == 404:
        # Two distinct 404 bodies were observed; they mean very different things.
        if "invalid route" in text:
            return "invalid_route"
        if "do not have data for those date(s)" in text or "not loaded yet" in text:
            return "no_data"
        return "not_found"
    if status == 429:
        return "rate_limited"
    if status >= 500:
        return "server"
    return "other"
