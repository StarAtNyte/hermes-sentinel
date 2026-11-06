"""Sliding-window rate limiting for alerts and notices."""

from __future__ import annotations

from collections import deque


class HourlyWindow:
    """Sliding one-hour window of send times (bounded by the cap)."""

    def __init__(self, cap: int) -> None:
        """Allow at most ``cap`` sends per rolling hour (0 suppresses everything)."""
        self.cap = max(0, int(cap))
        self._times: deque[float] = deque(maxlen=max(1, self.cap))

    def allow(self, now: float) -> bool:
        """Record a send at ``now`` and return True, or False when the cap is reached."""
        while self._times and now - self._times[0] >= 3600.0:
            self._times.popleft()
        if self.cap <= 0 or len(self._times) >= self.cap:
            return False
        self._times.append(now)
        return True
