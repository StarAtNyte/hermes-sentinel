"""Sliding window over the last few counted verdicts."""

from __future__ import annotations

from collections import deque

from .types import Observation


class ObservationWindow:
    """The last ``maxlen`` observations. Not thread-safe; the state machine's lock guards it."""

    def __init__(self, maxlen: int) -> None:
        """Create an empty window holding at most ``maxlen`` observations."""
        self._items: deque[Observation] = deque(maxlen=maxlen)

    def __len__(self) -> int:
        """Number of observations currently held."""
        return len(self._items)

    def add(self, observation: Observation) -> None:
        """Append an observation, evicting the oldest when full."""
        self._items.append(observation)

    def clear(self) -> None:
        """Forget every observation."""
        self._items.clear()

    def counts(self) -> tuple[int, int]:
        """``(hits, misses)`` currently in the window."""
        hits = sum(1 for o in self._items if o.hit)
        return hits, len(self._items) - hits

    def drop_leading_misses(self) -> None:
        """Drop misses from the front so an episode starts at its first hit."""
        while self._items and not self._items[0].hit:
            self._items.popleft()

    @property
    def first_ts(self) -> float:
        """Timestamp of the oldest observation (window must not be empty)."""
        return self._items[0].ts

    @property
    def last_is_hit(self) -> bool:
        """True if the newest observation was a hit (window must not be empty)."""
        return self._items[-1].hit

    def hit_confidences(self) -> list[float]:
        """Confidences of the hits, oldest first."""
        return [o.confidence for o in self._items if o.hit]
