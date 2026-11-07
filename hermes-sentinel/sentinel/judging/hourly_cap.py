"""A thread-safe sliding-window cap on how often something may happen per hour."""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable

__all__ = ["HourlyCap"]

WINDOW_SECONDS = 3600.0


class HourlyCap:
    """Allows at most ``limit`` acquisitions in any rolling hour.

    In memory only: a restart forgets it, but every acquisition is also a budgeted vision call,
    and the persisted budget is the hard ceiling.
    """

    def __init__(self, limit: int, *, clock: Callable[[], float] = time.monotonic) -> None:
        """``limit`` of 0 or less allows nothing; ``clock`` returns seconds."""
        self._limit = int(limit)
        self._clock = clock
        self._lock = threading.Lock()  # guards _stamps
        self._stamps: deque[float] = deque()

    def try_acquire(self) -> bool:
        """Take one slot if the rolling hour has room; ``False`` when the cap is reached."""
        now = self._clock()
        with self._lock:
            while self._stamps and now - self._stamps[0] >= WINDOW_SECONDS:
                self._stamps.popleft()
            if len(self._stamps) >= self._limit:
                return False
            self._stamps.append(now)
            return True

    def release(self) -> None:
        """Give back the most recent slot (the action it covered did not happen)."""
        with self._lock:
            if self._stamps:
                self._stamps.pop()

    def used(self) -> int:
        """Slots taken in the current rolling hour."""
        now = self._clock()
        with self._lock:
            return sum(1 for stamp in self._stamps if now - stamp < WINDOW_SECONDS)
