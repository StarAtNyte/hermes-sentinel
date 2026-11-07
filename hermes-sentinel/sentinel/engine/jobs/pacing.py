"""Per-rule pacing: the least gap between judge calls and the rolling hourly cap.

This is a safety net under the cooldown. Whatever keeps a gate firing (a person moving in
front of a webcam), one rule cannot spend more than ``min_interval`` seconds apart or
``calls_per_hour`` calls in any hour. A latched rule (fired, cooldown over, waiting for a
refuting verdict to re-arm) only needs to learn that the scene cleared, so it uses the longer
``latched_min_interval``.
"""

from __future__ import annotations

import threading
from collections.abc import Callable

from ...judging import HourlyCap

__all__ = ["RulePacing"]


class RulePacing:
    """Spacing and hourly cap of one rule's judge calls. Thread-safe."""

    def __init__(
        self,
        min_interval: float,
        calls_per_hour: int,
        *,
        clock: Callable[[], float],
        latched_min_interval: float = 0.0,
    ) -> None:
        """``min_interval`` seconds (0 turns spacing off); ``calls_per_hour`` is at least 1.

        ``latched_min_interval`` is the gap used while the rule is latched; it never drops
        below ``min_interval``.
        """
        self._min_interval = float(min_interval)
        self._latched_min_interval = max(float(latched_min_interval), self._min_interval)
        self._cap = HourlyCap(calls_per_hour, clock=clock)
        self._calls_per_hour = int(calls_per_hour)
        self._lock = threading.Lock()  # guards _last_call and _cap_notified
        self._last_call: float | None = None
        self._cap_notified = False

    @property
    def calls_per_hour(self) -> int:
        """The hourly cap."""
        return self._calls_per_hour

    def used(self) -> int:
        """Judge calls taken in the current rolling hour."""
        return self._cap.used()

    def spaced(self, now: float, *, latched: bool = False) -> bool:
        """True when the gap (the latched one if ``latched``) has passed since the last call."""
        gap = self._latched_min_interval if latched else self._min_interval
        with self._lock:
            last = self._last_call
        return last is None or now - last >= gap

    def has_room(self) -> bool:
        """True when the rolling hour still has a free call slot."""
        return self._cap.used() < self._calls_per_hour

    def begin_call(self, now: float, *, confirming: bool, latched: bool = False) -> bool:
        """Take a call slot at ``now``; ``False`` when the hourly cap or the spacing refuses.

        Confirmation looks have their own spacing, so only the cap applies to them.
        """
        if not confirming and not self.spaced(now, latched=latched):
            return False
        if not self._cap.try_acquire():
            return False
        with self._lock:
            self._last_call = now
            self._cap_notified = False
        return True

    def cancel_call(self) -> None:
        """Give back the slot of a call that was never made (the budget refused it)."""
        self._cap.release()

    def take_cap_notice(self) -> bool:
        """True once per cap episode, so the user hears about a reached cap a single time."""
        with self._lock:
            if self._cap_notified:
                return False
            self._cap_notified = True
            return True
