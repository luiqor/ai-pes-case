"""``YYYY-MM`` month arithmetic: validation, shifts, windows, data-start clamp.

Every date the pipeline reasons about is a *month* (pageviews are monthly by
default, windows are whole months), so month parsing and shifting live here
with exactly one implementation. :mod:`api` converts these months to the
v1 timestamp format; :mod:`common` re-exports every public name so callers
keep addressing them as ``common.<name>``.

No I/O and no settings -- this module is pure and sits at the bottom of the
import graph.
"""

from __future__ import annotations

import re
from datetime import date

# Verified from the API reference: pageview data starts 2015-07-01.
DATA_START_MONTH = "2015-07"

_MONTH_RE = re.compile(r"^(\d{4})-(\d{2})$")


def _validate_month(month: str, label: str) -> None:
    match = _MONTH_RE.match(month)
    if not match:
        raise ValueError(f"{label} must be 'YYYY-MM', got {month!r}")
    if not 1 <= int(match.group(2)) <= 12:
        raise ValueError(f"{label} has an invalid month number: {month!r}")


def parse_month(value: str) -> str:
    """Return ``value`` unchanged when it is a valid ``YYYY-MM`` month.

    Args:
        value: Candidate month string (e.g. from a CLI flag).

    Returns:
        The same string, so it can be used directly as an argparse ``type=``.

    Raises:
        ValueError: The value is malformed or names a month outside 01-12.
    """
    _validate_month(value, "month")
    return value


def month_to_stamp(month: str) -> str:
    """``2024-10`` -> ``2024100100`` (the v1 ``YYYYMMDDHH`` parameter)."""
    match = _MONTH_RE.match(month)
    if not match:
        raise ValueError(f"month must be 'YYYY-MM', got {month!r}")
    return f"{match.group(1)}{match.group(2)}0100"


def month_range(since: str, until: str) -> list[str]:
    """Inclusive list of ``YYYY-MM`` strings from ``since`` to ``until``."""
    _validate_month(since, "since")
    _validate_month(until, "until")
    if since > until:
        raise ValueError(f"since ({since}) is after until ({until})")
    year, month = int(since[:4]), int(since[5:7])
    end_year, end_month = int(until[:4]), int(until[5:7])
    out: list[str] = []
    while (year, month) <= (end_year, end_month):
        out.append(f"{year:04d}-{month:02d}")
        month += 1
        if month == 13:
            month, year = 1, year + 1
    return out


def shift_month(month: str, delta: int) -> str:
    """Shift a ``YYYY-MM`` month by ``delta`` months (negative = backwards).

    Args:
        month: Anchor month in ``YYYY-MM`` form.
        delta: Number of months to move (may cross year boundaries).

    Returns:
        The shifted month in ``YYYY-MM`` form.

    Raises:
        ValueError: If ``month`` is malformed.
    """
    _validate_month(month, "month")
    year, mon = int(month[:4]), int(month[5:7])
    index = year * 12 + (mon - 1) + delta
    return f"{index // 12:04d}-{index % 12 + 1:02d}"


def last_complete_month(today: date | None = None) -> str:
    """The most recent month that has fully elapsed.

    The current month is always partial, so it is never a valid window end.
    Note that Wikimedia may still be loading the previous month (verified:
    "usually within hours, up to 24 h+ on problems"), so callers must also
    tolerate months being absent from a response.
    """
    current = today or date.today()
    return shift_month(f"{current.year:04d}-{current.month:02d}", -1)


def default_window(months: int = 24, today: date | None = None) -> tuple[str, str]:
    """``(since, until)`` covering ``months`` complete months ending last month."""
    if months < 2:
        raise ValueError("window must be at least 2 months")
    until = last_complete_month(today)
    return shift_month(until, -(months - 1)), until


def clamp_to_data_start(since: str) -> str:
    """Move ``since`` forward to ``DATA_START_MONTH`` when it predates it.

    Pageview data exists from 2015-07 only (verified), so an earlier start
    can never be honoured and is clamped with a warning upstream.
    """
    return max(since, DATA_START_MONTH)
