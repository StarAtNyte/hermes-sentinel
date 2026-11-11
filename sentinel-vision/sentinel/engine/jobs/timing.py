"""Pure timing rules for a rule: its schedule window and the spacing of confirmation looks."""

from __future__ import annotations

import logging
from datetime import datetime

from ...rules import RuleSpec

logger = logging.getLogger(__name__)

__all__ = ["confirm_spacing", "schedule_active"]


def schedule_active(spec: RuleSpec, wall_now: float) -> bool:
    """True when the rule has no schedule, the schedule is active now, or it cannot be read."""
    if spec.schedule is None:
        return True
    try:
        return spec.schedule.is_active(datetime.fromtimestamp(wall_now))
    except (ValueError, OverflowError, OSError, TypeError) as exc:
        logger.warning("sentinel: rule %s has an unreadable schedule: %s", spec.id, exc)
        return True


def confirm_spacing(spec: RuleSpec, confirm_interval: float) -> float:
    """Seconds between the extra looks of a rule collecting k-of-n confirmations."""
    hold = spec.effective_hold_seconds
    steps = max(1, spec.confirm_n - 1)
    return max(confirm_interval, hold / steps if hold > 0 else 0.0)
