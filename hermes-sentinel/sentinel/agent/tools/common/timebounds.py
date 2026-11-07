"""Time bounds for event searches: ISO-8601, ``today`` and relative ``2h`` forms."""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any

from .results import ToolError

__all__ = ["parse_time_bound"]

_REL_RE = re.compile(
    r"^\s*(\d+(?:\.\d+)?)\s*(s|sec|secs|m|min|mins|h|hr|hrs|d|day|days|w|wk|"
    r"week|weeks)\s*(?:ago)?\s*$",
    re.IGNORECASE,
)
_REL_UNITS = {
    "s": 1,
    "sec": 1,
    "secs": 1,
    "m": 60,
    "min": 60,
    "mins": 60,
    "h": 3600,
    "hr": 3600,
    "hrs": 3600,
    "d": 86400,
    "day": 86400,
    "days": 86400,
    "w": 604800,
    "wk": 604800,
    "week": 604800,
    "weeks": 604800,
}


def parse_time_bound(value: Any, now: datetime | None = None) -> datetime | None:
    """``None``, ISO-8601, ``today``/``yesterday``/``now`` or a relative ``2h`` / ``3 days ago``.

    Naive ISO times are local. Returns an aware datetime. Raises :class:`ToolError`.
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if not isinstance(value, str):
        raise ToolError("time bounds must be strings")
    now = now or datetime.now().astimezone()
    text = value.strip().lower()
    named = _named_bound(text, now)
    if named is not None:
        return named
    m = _REL_RE.match(text)
    if m:
        seconds = float(m.group(1)) * _REL_UNITS[m.group(2).lower()]
        return now - timedelta(seconds=min(seconds, 3650 * 86400))
    try:
        when = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        raise ToolError(
            f"cannot understand time {value!r}; use ISO-8601, 'today', 'yesterday' or '2h'"
        ) from None
    return when.astimezone() if when.tzinfo is None else when


def _named_bound(text: str, now: datetime) -> datetime | None:
    """The bound for words such as ``today`` or ``last hour``, ``None`` for anything else."""
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if text == "now":
        return now
    if text in ("today", "this morning"):
        return midnight
    if text == "yesterday":
        return midnight - timedelta(days=1)
    if text in ("last hour", "past hour"):
        return now - timedelta(hours=1)
    if text in ("this week", "last week", "past week"):
        return now - timedelta(days=7)
    return None
