"""Importable test constants and recorded-fixture lookup.

Deliberately a plain module, not ``conftest``: pytest manages ``conftest`` and
importing from it is not a supported interface. Fixtures live in
``conftest.py``; shared *data* lives here.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

FIXTURES = Path(__file__).resolve().parent / "fixtures"

# The window used for the recorded golden data. Keep in sync with
# tests/record_fixtures.py if the fixtures are ever refreshed.
GOLDEN_SINCE = "2024-10"
GOLDEN_UNTIL = "2026-08"
GOLDEN_TODAY = date(2026, 9, 24)


def _manifest() -> dict[str, str]:
    path = FIXTURES / "manifest.json"
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


MANIFEST = _manifest()


def load_fixture(url: str) -> Any:
    name = MANIFEST.get(url)
    if name is None:
        raise AssertionError(
            f"unrecorded URL requested: {url}\n"
            "Run `uv run python tests/record_fixtures.py` to refresh fixtures."
        )
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))
