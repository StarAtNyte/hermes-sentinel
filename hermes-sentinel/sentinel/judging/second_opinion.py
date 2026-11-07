"""Second opinion for borderline positive verdicts."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from ..rules import RuleSpec, RuleState
from ..rules.machine.evidence import verdict_to_observation
from ..vision.judge import Verdict, combine_verdicts
from .hourly_cap import HourlyCap
from .policy import JudgingPolicy

__all__ = ["SecondOpinion", "padded_crop"]

logger = logging.getLogger(__name__)

#: Fraction of the changed region's size added on every side of the crop.
CROP_PADDING = 0.25
#: A crop is never smaller than this (pixels), so a tiny change still has context.
MIN_CROP_SIDE = 96
#: States in which a positive verdict cannot start or confirm an alert.
_NO_EFFECT_STATES = frozenset({RuleState.FIRING, RuleState.COOLDOWN})


def padded_crop(frame: Any, bbox: tuple[int, int, int, int] | None) -> Any:
    """The part of ``frame`` around ``bbox`` (``x, y, w, h`` pixels), padded and clamped.

    Returns ``frame`` itself when there is no box (the second look then just uses the
    larger encoding size and a different prompt).
    """
    if bbox is None:
        return frame
    height, width = frame.shape[:2]
    x, y, w, h = bbox
    pad = int(max(w, h) * CROP_PADDING)
    half_w = max(w + 2 * pad, MIN_CROP_SIDE) // 2
    half_h = max(h + 2 * pad, MIN_CROP_SIDE) // 2
    cx, cy = x + w // 2, y + h // 2
    x0, x1 = max(0, cx - half_w), min(width, cx + half_w)
    y0, y1 = max(0, cy - half_h), min(height, cy + half_h)
    if x1 - x0 < 2 or y1 - y0 < 2:
        return frame
    return frame[y0:y1, x0:x1]


class SecondOpinion:
    """Re-checks a borderline verdict that would start or confirm an alert.

    Every extra check reserves the vision budget and one slot of an hourly cap first; when
    either refuses, or the second look fails, the first verdict stands as it was.
    """

    def __init__(
        self,
        judge: Any,
        policy: JudgingPolicy,
        cap: HourlyCap,
        reserve: Callable[[RuleSpec], bool],
        stopped: Callable[[], bool],
    ) -> None:
        """``reserve`` consumes one vision call for a rule; ``stopped`` is the shutdown flag."""
        self._judge = judge
        self._policy = policy
        self._cap = cap
        self._reserve = reserve
        self._stopped = stopped

    def wanted(self, spec: RuleSpec, state: RuleState, verdict: Verdict) -> bool:
        """True when ``verdict`` is a borderline hit that would move the rule toward an alert."""
        policy = self._policy
        if not policy.second_opinion_enabled or not verdict.ok or state in _NO_EFFECT_STATES:
            return False
        if spec.motion:
            return False  # one still crop cannot show movement, so it would overturn real hits
        if spec.confidence_threshold >= policy.band_high:
            return False  # every hit of this rule is already above the band
        if not policy.band_low <= verdict.confidence < policy.band_high:
            return False
        observation = verdict_to_observation(spec, verdict.match, verdict.confidence, 0.0)
        return observation is not None and observation.hit

    def review(
        self,
        spec: RuleSpec,
        state: RuleState,
        frame: Any,
        bbox: tuple[int, int, int, int] | None,
        verdict: Verdict,
    ) -> Verdict:
        """The verdict to feed the rule: ``verdict`` itself, or merged with a second opinion."""
        if not self.wanted(spec, state, verdict) or self._stopped():
            return verdict
        if not self._cap.try_acquire():
            return verdict
        if not self._reserve(spec):
            self._cap.release()
            return verdict
        try:
            crop = padded_crop(frame, bbox)
        except (AttributeError, IndexError, TypeError, ValueError) as exc:
            logger.warning("sentinel: no crop for second opinion on %s: %s", spec.id, exc)
            crop = frame
        second = self._judge.verify(crop, spec.question, rule_id=spec.id)
        return combine_verdicts(verdict, second)
