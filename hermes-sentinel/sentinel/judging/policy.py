"""The judging knobs taken from the plugin config."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..vision.judge.batch import MAX_BATCH_RULES

__all__ = ["JudgingPolicy"]


@dataclass(frozen=True)
class JudgingPolicy:
    """How checks are grouped and verified. Defaults match ``plugin.yaml``."""

    motion_frames: int = 3
    batching: bool = True
    batch_max_rules: int = 4
    second_opinion: bool = True
    band_low: float = 0.5
    band_high: float = 0.8
    second_opinion_per_hour: int = 6

    @classmethod
    def from_config(cls, config: Any) -> JudgingPolicy:
        """Build the policy from a :class:`~sentinel.config.SentinelConfig`."""
        return cls(
            motion_frames=config.motion_frames,
            batching=config.batch_judging,
            batch_max_rules=config.batch_max_rules,
            second_opinion=config.second_opinion,
            band_low=config.second_opinion_band_low,
            band_high=config.second_opinion_band_high,
            second_opinion_per_hour=config.second_opinion_per_hour,
        )

    @property
    def batch_cap(self) -> int:
        """Most rules answered in one call (1 means batching is off); never above the ceiling."""
        if not self.batching:
            return 1
        return max(1, min(int(self.batch_max_rules), MAX_BATCH_RULES))

    @property
    def second_opinion_enabled(self) -> bool:
        """True when second opinions can run at all."""
        return self.second_opinion and self.second_opinion_per_hour > 0
