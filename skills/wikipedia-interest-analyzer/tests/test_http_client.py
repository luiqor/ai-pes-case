"""The production transport (``http_client.WikiClient``), fully offline.

The session is injected, so these tests exercise the *real* request path --
retries, backoff, cache reads/writes, classification -- without any network
being possible. A scripted session also makes the politeness behaviour
observable: sleeps are recorded, not performed.
"""

from __future__ import annotations

from typing import Any

import common
import http_client
import pytest


class _FakeResponse:
    """Minimal ``requests.Response`` stand-in."""

    def __init__(self, status_code: int, payload: Any = None, text: str = "") -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = text or repr(payload)

    def json(self) -> Any:
        if self._payload is None:
            raise ValueError("no JSON body")
        return self._payload


class _FakeSession:
    """Scripted ``requests.Session``: answers in order, records every call."""

    def __init__(self, *responses: _FakeResponse) -> None:
        self._responses = list(responses)
        self.calls: list[str] = []
        self.closed = False

    def get(self, url: str, *, timeout: float | None = None) -> _FakeResponse:
        self.calls.append(url)
        return self._responses.pop(0)

    def close(self) -> None:
        self.closed = True


def _client(
    session: _FakeSession, sleeps: list[float], tmp_path
) -> http_client.WikiClient:
    """Client under test: scripted session, recorded sleeps, private cache."""
    return http_client.WikiClient(
        cache_dir=tmp_path / "cache", session=session, sleep=sleeps.append
    )


def test_get_json_returns_the_body_then_serves_it_from_cache(tmp_path):
    """A cached response must not touch the network (or the cadence)."""
    url = "https://example.test/metrics/x"
    session = _FakeSession(_FakeResponse(200, {"items": [1]}))
    client = _client(session, [], tmp_path)

    assert client.get_json(url) == {"items": [1]}
    assert client.get_json(url) == {"items": [1]}
    assert session.calls == [url], "the second call must come from the cache"


def test_transient_5xx_is_retried_with_backoff_and_then_succeeds(tmp_path):
    session = _FakeSession(
        _FakeResponse(503, {"detail": "unavailable"}),
        _FakeResponse(200, {"ok": 1}),
    )
    sleeps: list[float] = []
    client = _client(session, sleeps, tmp_path)

    assert client.get_json("https://example.test/y") == {"ok": 1}
    assert len(session.calls) == 2, "5xx is transient: exactly one retry"
    assert any(s >= 1.0 for s in sleeps), "the retry must back off, not hammer"


def test_deterministic_404_fails_immediately_and_is_classified(tmp_path):
    """Retrying a deterministic 4xx cannot help; ``kind`` must locate it."""
    session = _FakeSession(_FakeResponse(404, {"detail": "weird body"}))
    client = _client(session, [], tmp_path)

    with pytest.raises(common.ApiError) as excinfo:
        client.get_json("https://example.test/z")
    assert excinfo.value.kind == "not_found"
    assert len(session.calls) == 1, "no retry for a deterministic failure"


def test_html_served_with_200_is_a_bad_body_not_a_crash(tmp_path):
    session = _FakeSession(_FakeResponse(200, None, text="<html>login page</html>"))
    client = _client(session, [], tmp_path)

    with pytest.raises(common.ApiError) as excinfo:
        client.get_json("https://example.test/proxied")
    assert excinfo.value.kind == "bad_body"
    assert len(session.calls) == 1


def test_context_manager_closes_the_session(tmp_path):
    """Mains own their client via ``with``; closing must actually release it."""
    session = _FakeSession()
    with http_client.WikiClient(
        cache_dir=tmp_path / "cache", session=session
    ) as client:
        assert client is not None
    assert session.closed
