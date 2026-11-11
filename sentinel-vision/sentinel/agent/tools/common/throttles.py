"""Small thread-safe counters: an hourly cap and a bounded once-per-key ledger."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

__all__ = ["HourlyCap", "OncePerKey"]


class HourlyCap:
    """Tiny thread-safe rolling-hour counter."""

    def __init__(self, cap: int, clock: Callable[[], float] = time.monotonic) -> None:
        """Allow ``cap`` uses per rolling hour, timed by ``clock``."""
        self.cap = max(0, int(cap))
        self._clock = clock
        self._lock = threading.Lock()  # guards _times
        self._times: list[float] = []

    def take(self) -> bool:
        """Count one use; ``False`` (and no count) when the last hour is already at the cap."""
        now = self._clock()
        with self._lock:
            self._times = [t for t in self._times if now - t < 3600.0]
            if len(self._times) >= self.cap:
                return False
            self._times.append(now)
            return True


class OncePerKey:
    """Remembers up to ``limit`` keys so a hint is shown once per session (reset when full)."""

    def __init__(self, limit: int = 1024) -> None:
        """Remember at most ``limit`` keys before starting over."""
        self._limit = limit
        self._lock = threading.Lock()  # guards _seen
        self._seen: set[str] = set()

    def first_time(self, key: str) -> bool:
        """``True`` the first time ``key`` is seen, ``False`` afterwards."""
        with self._lock:
            if key in self._seen:
                return False
            self._seen.add(key)
            if len(self._seen) > self._limit:
                self._seen.clear()
            return True
