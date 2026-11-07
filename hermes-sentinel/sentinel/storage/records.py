"""Record dataclasses and the UTC time helpers the store uses."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Any


def _to_datetime(when: datetime | float | int | None) -> datetime:
    """Normalise a timestamp-like value into an aware UTC datetime."""
    if when is None:
        return datetime.now(UTC)
    if isinstance(when, datetime):
        return when.astimezone(UTC) if when.tzinfo else when.replace(tzinfo=UTC)
    if not isinstance(when, (int, float)) or isinstance(when, bool):
        raise TypeError(f"unsupported timestamp {when!r}")
    if not math.isfinite(float(when)):
        raise ValueError("timestamp must be finite")
    return datetime.fromtimestamp(float(when), UTC)


def to_iso(when: datetime | float | int | None = None) -> str:
    """Fixed-width ISO-8601 UTC (millisecond precision, ``Z`` suffix)."""
    dt = _to_datetime(when)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def parse_ts(value: str) -> datetime:
    """Parse an ISO-8601 string (``Z`` or offset; naive = UTC) into an aware UTC datetime."""
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    return dt.astimezone(UTC) if dt.tzinfo else dt.replace(tzinfo=UTC)


def as_iso(when: datetime | float | int | str | None) -> str | None:
    """``to_iso`` that also accepts ISO strings and passes ``None`` through."""
    if when is None:
        return None
    if isinstance(when, str):
        return to_iso(parse_ts(when))
    return to_iso(when)


def hour_bucket(when: datetime | float | int) -> str:
    """``YYYY-MM-DDTHH`` (UTC) bucket for usage counters."""
    return to_iso(when)[:13]


def day_range(day: datetime) -> tuple[str, str]:
    """``(since, until)`` ISO bounds for the UTC day containing ``day``."""
    start = day.astimezone(UTC) if day.tzinfo else day.replace(tzinfo=UTC)
    start = start.replace(hour=0, minute=0, second=0, microsecond=0)
    return to_iso(start), to_iso(start + timedelta(days=1))


def clip(value: Any, limit: int) -> str:
    """``str(value)`` truncated to ``limit`` characters (``None`` becomes ``""``)."""
    text = "" if value is None else str(value)
    return text[:limit]


@dataclass(frozen=True)
class EventRecord:
    """One confirmed event. ``snapshot_path`` is absolute (or ``None`` once pruned)."""

    id: int
    rule_id: str
    ts: str
    caption: str
    reason: str
    confidence: float | None
    source: str
    snapshot_path: str | None
    feedback: str | None
    delivery: str | None = None
    delivery_detail: str = ""
    delivery_ts: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Plain-dict form for JSON replies."""
        return asdict(self)


@dataclass(frozen=True)
class ExampleRecord:
    """A teaching example image attached to a rule."""

    id: int
    rule_id: str
    kind: str
    path: str
    event_id: int | None
    note: str
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        """Plain-dict form for JSON replies."""
        return asdict(self)


@dataclass(frozen=True)
class UsageResult:
    """Outcome of :meth:`Store.try_consume_usage`."""

    allowed: bool
    hour_used: int
    day_used: int


@dataclass
class PruneReport:
    """What :meth:`Store.prune` removed."""

    events_deleted: int = 0
    snapshots_deleted: int = 0
    bytes_freed: int = 0
    usage_rows_deleted: int = 0
    orphans_deleted: int = 0
    bytes_after: int = 0

    def to_dict(self) -> dict[str, Any]:
        """Plain-dict form for JSON replies."""
        return asdict(self)
