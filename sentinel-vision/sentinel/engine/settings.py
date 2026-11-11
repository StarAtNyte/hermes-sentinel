"""Internal timing knobs of the watch engine."""

from __future__ import annotations

import math
from dataclasses import dataclass

__all__ = ["EngineSettings"]

_POSITIVE_FIELDS = (
    "capture_interval",
    "confirm_interval",
    "backoff_initial",
    "backoff_max",
    "judge_backoff_initial",
    "judge_backoff_max",
    "scheduler_interval",
    "sync_interval",
    "stop_timeout",
    "max_frame_age",
    "judge_error_rearm",
)


@dataclass(frozen=True)
class EngineSettings:
    """Internal timing knobs (not user config; tests shorten them)."""

    capture_interval: float = 0.5
    confirm_interval: float = 2.0
    queue_size: int = 8
    workers: int = 2
    backoff_initial: float = 1.0
    backoff_max: float = 60.0
    offline_grace: float = 5.0
    judge_backoff_initial: float = 5.0
    judge_backoff_max: float = 300.0
    scheduler_interval: float = 1.0
    sync_interval: float = 30.0
    stop_timeout: float = 5.0
    max_frame_age: float = 5.0
    judge_error_rearm: float = 600.0

    def __post_init__(self) -> None:
        """Reject non-positive timings and out-of-range pool sizes."""
        for name in _POSITIVE_FIELDS:
            value = getattr(self, name)
            if not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be a positive number")
        if self.offline_grace < 0:
            raise ValueError("offline_grace must be >= 0")
        if not 1 <= int(self.queue_size) <= 1024:
            raise ValueError("queue_size must be 1..1024")
        if not 1 <= int(self.workers) <= 16:
            raise ValueError("workers must be 1..16")
