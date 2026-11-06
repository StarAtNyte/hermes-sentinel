"""Vision-call budget: per-hour and per-day caps with counters persisted in the store.

* The hour cap applies to the current UTC clock hour.
* The day cap applies to a rolling 24 hours made of the current hour bucket and the 23 before
  it, so it is timezone-agnostic and frees capacity hour by hour.
* Counters live in the ``usage`` table (:meth:`Store.try_consume_usage`), so they survive
  restarts and are shared with any other process using the same database.
* A cap of ``None`` means unlimited; ``0`` means no vision calls at all (judging paused).

When a call is refused the engine pauses judging; :meth:`Budget.take_exhausted_notice`
returns a user-facing notice exactly once per exhaustion episode (it re-arms after a call
is allowed again), so the agent is told once rather than on every refused frame.
"""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Any, Protocol

__all__ = [
    "DEFAULT_PER_DAY",
    "DEFAULT_PER_HOUR",
    "Budget",
    "BudgetStatus",
    "UsageBackend",
    "parse_cap",
]

DEFAULT_PER_HOUR = 60
DEFAULT_PER_DAY = 500
MAX_CAP = 1_000_000


class UsageBackend(Protocol):
    """The two store methods the budget needs (implemented by :class:`store.Store`)."""

    def try_consume_usage(
        self,
        n: int = 1,
        *,
        hour_cap: int | None,
        day_cap: int | None,
        rule_id: str = "",
        now: float | None = None,
    ) -> Any: ...

    def usage_totals(
        self, now: float | None = None, *, rule_id: str | None = None
    ) -> tuple[int, int]: ...


def parse_cap(value: Any, default: int | None) -> int | None:
    """Coerce a config value to a cap. ``None``/missing -> ``default``; negative, non-numeric
    or non-finite -> ``default``; values above ``MAX_CAP`` are clamped."""
    if value is None or isinstance(value, bool):
        return default
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("unlimited", "none", "off"):
            return None
        try:
            value = float(text)
        except ValueError:
            return default
    if not isinstance(value, (int, float)):
        return default
    value = float(value)
    if not math.isfinite(value) or value < 0:
        return default
    return min(int(value), MAX_CAP)


@dataclass(frozen=True)
class BudgetStatus:
    """Snapshot of budget usage."""

    hour_used: int
    hour_cap: int | None
    day_used: int
    day_cap: int | None
    exhausted: bool
    retry_after_seconds: float

    @property
    def hour_remaining(self) -> int | None:
        return None if self.hour_cap is None else max(0, self.hour_cap - self.hour_used)

    @property
    def day_remaining(self) -> int | None:
        return None if self.day_cap is None else max(0, self.day_cap - self.day_used)

    def to_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["hour_remaining"] = self.hour_remaining
        out["day_remaining"] = self.day_remaining
        return out

    def describe(self) -> str:
        def part(used: int, cap: int | None, label: str) -> str:
            return f"{used}/{'unlimited' if cap is None else cap} {label}"

        text = (
            f"Vision checks: {part(self.hour_used, self.hour_cap, 'this hour')}, "
            f"{part(self.day_used, self.day_cap, 'in the last 24 h')}."
        )
        if self.exhausted:
            text += f" Paused; resumes in about {math.ceil(self.retry_after_seconds / 60)} min."
        return text


def _seconds_to_next_hour(now: float) -> float:
    return 3600.0 - (now % 3600.0)


class Budget:
    """Thread-safe vision-call budget over a :class:`UsageBackend`."""

    def __init__(
        self,
        backend: UsageBackend,
        *,
        per_hour: int | None = DEFAULT_PER_HOUR,
        per_day: int | None = DEFAULT_PER_DAY,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._backend = backend
        self._clock = clock
        self._lock = threading.Lock()
        self._per_hour: int | None = None
        self._per_day: int | None = None
        self._exhausted = False
        self._notice_sent = False
        self.set_caps(per_hour=per_hour, per_day=per_day)

    @property
    def per_hour(self) -> int | None:
        return self._per_hour

    @property
    def per_day(self) -> int | None:
        return self._per_day

    def set_caps(self, *, per_hour: int | None, per_day: int | None) -> None:
        """Change the caps (e.g. after a config edit). Raises ``ValueError`` if negative."""
        for name, cap in (("per_hour", per_hour), ("per_day", per_day)):
            if cap is not None and (isinstance(cap, bool) or not isinstance(cap, int) or cap < 0):
                raise ValueError(f"{name} must be a non-negative int or None, got {cap!r}")
        with self._lock:
            self._per_hour, self._per_day = per_hour, per_day

    def try_consume(self, n: int = 1, *, rule_id: str = "") -> bool:
        """Reserve ``n`` vision calls. ``True`` if allowed (and recorded)."""
        now = self._clock()
        with self._lock:
            hour_cap, day_cap = self._per_hour, self._per_day
        result = self._backend.try_consume_usage(
            n, hour_cap=hour_cap, day_cap=day_cap, rule_id=rule_id, now=now
        )
        allowed = bool(getattr(result, "allowed", result))
        with self._lock:
            if allowed:
                self._exhausted = False
                self._notice_sent = False
            else:
                self._exhausted = True
        return allowed

    def status(self) -> BudgetStatus:
        now = self._clock()
        hour_used, day_used = self._backend.usage_totals(now)
        with self._lock:
            hour_cap, day_cap = self._per_hour, self._per_day
        exhausted = (hour_cap is not None and hour_used >= hour_cap) or (
            day_cap is not None and day_used >= day_cap
        )
        return BudgetStatus(
            hour_used=hour_used,
            hour_cap=hour_cap,
            day_used=day_used,
            day_cap=day_cap,
            exhausted=exhausted,
            retry_after_seconds=(
                self._retry_after(now, hour_used, hour_cap, day_used, day_cap)
                if exhausted
                else 0.0
            ),
        )

    def _retry_after(
        self, now: float, hour_used: int, hour_cap: int | None, day_used: int,
        day_cap: int | None,
    ) -> float:
        """Seconds until every exhausted cap has room again."""
        wait = 0.0
        if hour_cap is not None and hour_used >= hour_cap:
            wait = _seconds_to_next_hour(now)
        if day_cap is not None and day_used >= day_cap:
            wait = max(wait, self._day_retry(now, day_used, day_cap))
        return wait

    def _day_retry(self, now: float, day_used: int, day_cap: int) -> float:
        """When enough of the oldest hourly buckets leave the 24 h window to drop below the cap."""
        fallback = _seconds_to_next_hour(now)
        buckets_fn = getattr(self._backend, "usage_buckets", None)
        if buckets_fn is None or day_cap <= 0:
            return fallback
        try:
            buckets = buckets_fn(now)
        except Exception:  # noqa: BLE001 - a store hiccup must not break status
            return fallback
        used = day_used
        for start, calls in buckets:
            used -= calls
            if used < day_cap:
                return max(0.0, start + 24 * 3600 - now)
        return fallback

    def take_exhausted_notice(self) -> str | None:
        """A notice to post once per exhaustion episode, else ``None``."""
        with self._lock:
            if not self._exhausted or self._notice_sent:
                return None
            self._notice_sent = True
        return "Sentinel paused vision checks: budget reached. " + self.status().describe()
