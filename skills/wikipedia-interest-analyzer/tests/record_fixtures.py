"""Record live API responses into ``tests/fixtures/`` so the suite runs offline.

Run manually whenever the golden data should be refreshed:

    uv run python tests/record_fixtures.py

It drives the *production* code paths (``resolve.resolve`` + ``fetch.fetch_series``)
with a recording client injected to capture every URL actually requested, then
writes one fixture file per URL plus ``manifest.json`` mapping URL -> filename.
Tests load by URL from that manifest, so a test can never silently hit the
network: an unrecorded URL raises instead.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from datetime import date
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import common  # noqa: E402
import fetch as fetch_mod  # noqa: E402
import http_client  # noqa: E402
import resolve as resolve_mod  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"

# Fixed study used for recording; keep in sync with the golden assertions in
# tests/test_analyze.py.
STUDY = {
    "version": 1,
    "topic": "intermittent fasting",
    "languages": ["pl", "cs"],
    "window": {"since": "2024-10", "until": "2026-08"},
    "access": common.DEFAULT_ACCESS,
    "agent": common.DEFAULT_AGENT,
    "granularity": "monthly",
    "overrides": {"pl": "Post"},
    "resolution": None,
}


def slug(url: str) -> str:
    path, _, _query = url.partition("?")
    path = re.sub(r"^https://", "", path)
    path = path.replace("/api/rest_v1/metrics/", "")
    path = path.replace("/w/api.php", "action")
    path = path.replace("www.wikidata.org", "wikidata")
    path = path.replace("/", "_")
    path = re.sub(r"[^A-Za-z0-9._-]+", "_", path)
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:8]
    return f"{path}__{digest}.json"


def main() -> int:
    FIXTURES.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, str] = {}

    class RecordingClient:
        """Real transport that saves every body it receives as a fixture."""

        def __init__(self) -> None:
            self._inner = http_client.WikiClient()

        def get_json(self, url: str) -> Any:
            body = self._inner.get_json(url)
            name = slug(url)
            (FIXTURES / name).write_text(
                json.dumps(body, ensure_ascii=False, indent=1), encoding="utf-8"
            )
            manifest[url] = name
            print(f"recorded {name}")
            return body

    client = RecordingClient()
    study = dict(STUDY)
    study["resolution"] = resolve_mod.resolve(
        study["topic"], study["languages"], search_language="en", client=client
    )
    series = fetch_mod.fetch_series(study, today=date(2026, 9, 24), client=client)

    # A deliberately misspelled title, to pin the observed `missing` response
    # that makes an override fail loudly instead of reporting zero interest.
    resolve_mod.confirm_titles("pl", ["Głódówka lecznicza"], client=client)

    common.write_json(FIXTURES / "series.expected.json", series)
    (FIXTURES / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"\n{len(manifest)} URLs recorded -> {FIXTURES}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
