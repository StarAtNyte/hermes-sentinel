"""The active-hours window of a rule."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .errors import RuleValidationError
from .validation import parse_day, parse_hhmm

ALL_DAYS: tuple[int, ...] = (0, 1, 2, 3, 4, 5, 6)


@dataclass(frozen=True)
class Schedule:
    """Active window for a rule, evaluated against a caller-supplied (local) datetime.

    ``start``/``end`` are ``"HH:MM"``; ``end`` may be ``"24:00"``. When ``end < start`` the
    window crosses midnight and the part after midnight belongs to the previous day's window
    (``days`` names the day the window *starts*). ``start == end`` means all day. ``days``
    uses 0 = Monday .. 6 = Sunday.
    """

    start: str = "00:00"
    end: str = "24:00"
    days: tuple[int, ...] = ALL_DAYS

    def __post_init__(self) -> None:
        """Validate the times and normalise ``days`` to a sorted tuple."""
        parse_hhmm(self.start, "schedule.start", allow_24=False)
        parse_hhmm(self.end, "schedule.end", allow_24=True)
        days = tuple(sorted({parse_day(d) for d in self.days}))
        if not days:
            raise RuleValidationError("schedule.days must name at least one weekday")
        object.__setattr__(self, "days", days)

    def is_active(self, when: datetime) -> bool:
        """True if ``when`` (naive local or aware) falls inside the window."""
        start = parse_hhmm(self.start, "schedule.start", allow_24=False)
        end = parse_hhmm(self.end, "schedule.end", allow_24=True)
        minute = when.hour * 60 + when.minute
        day = when.weekday()
        if start == end or (start == 0 and end == 24 * 60):
            return day in self.days
        if start < end:
            return day in self.days and start <= minute < end
        # Overnight window, e.g. 22:00-06:00.
        return (day in self.days and minute >= start) or (
            (day - 1) % 7 in self.days and minute < end
        )

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe form (``days`` as a list)."""
        return {"start": self.start, "end": self.end, "days": list(self.days)}

    @classmethod
    def from_dict(cls, data: Any) -> Schedule:
        """Build a schedule from a mapping (or return an existing one unchanged)."""
        if isinstance(data, Schedule):
            return data
        if not isinstance(data, Mapping):
            raise RuleValidationError(f"schedule must be an object, got {type(data).__name__}")
        days = data.get("days")
        if days is None:
            days = ALL_DAYS
        elif isinstance(days, (str, int)):
            days = (days,)
        elif not isinstance(days, Sequence):
            raise RuleValidationError("schedule.days must be a list")
        return cls(
            start=str(data.get("start") or "00:00").strip(),
            end=str(data.get("end") or "24:00").strip(),
            days=tuple(days),
        )
