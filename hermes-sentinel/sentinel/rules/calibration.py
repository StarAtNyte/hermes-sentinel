"""Pure per-rule threshold tuning from user feedback.

A false alarm raises the rule's confidence threshold by a clear step. A confirmed-correct
alert lowers it by a much smaller step, never below a floor, so a rule that keeps being right
slowly becomes more sensitive without ever drifting into noise.
"""

from __future__ import annotations

__all__ = [
    "CORRECT_STEP",
    "FALSE_ALARM_STEP",
    "MAX_TUNED_THRESHOLD",
    "MIN_TUNED_THRESHOLD",
    "lowered_threshold",
    "raised_threshold",
]

FALSE_ALARM_STEP = 0.05
CORRECT_STEP = 0.01
MAX_TUNED_THRESHOLD = 0.95
#: Feedback never lowers a threshold below this (a rule set lower by hand is left alone).
MIN_TUNED_THRESHOLD = 0.6


def raised_threshold(current: float) -> float:
    """Threshold after a false alarm: one step up, capped at ``MAX_TUNED_THRESHOLD``."""
    return min(MAX_TUNED_THRESHOLD, round(current + FALSE_ALARM_STEP, 3))


def lowered_threshold(current: float) -> float:
    """Threshold after a confirmed-correct alert: one small step down, bounded below.

    Returns ``current`` unchanged when it is already at or under ``MIN_TUNED_THRESHOLD``.
    """
    if current <= MIN_TUNED_THRESHOLD:
        return current
    return max(MIN_TUNED_THRESHOLD, round(current - CORRECT_STEP, 3))
