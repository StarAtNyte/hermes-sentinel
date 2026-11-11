"""Pure conversion of a judge verdict into counted evidence."""

from __future__ import annotations

import math
from typing import Any

from ..constants import MODE_ABSENCE
from ..spec import RuleSpec
from .types import Observation


def verdict_to_observation(
    spec: RuleSpec, match: bool | None, confidence: Any, now: float
) -> Observation | None:
    """Evidence from one verdict, or ``None`` if it is unusable (error, non-finite, ...).

    For ``absence`` rules a confident *negative* is the evidence.
    """
    if match is None or not isinstance(match, bool):
        return None
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        return None
    conf = float(confidence)
    if not math.isfinite(conf):
        return None
    conf = min(1.0, max(0.0, conf))
    evidence = (not match) if spec.mode == MODE_ABSENCE else match
    hit = evidence and conf >= spec.confidence_threshold
    return Observation(ts=now, hit=hit, confidence=conf)
