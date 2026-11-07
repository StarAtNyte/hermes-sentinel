"""Threshold tuning from feedback: big steps up, small bounded steps down."""

from __future__ import annotations

import pytest
from harness.plugin_loader import plugin_module

calibration = plugin_module("sentinel.rules.calibration")


def test_false_alarm_raises_by_a_step_and_caps():
    assert calibration.raised_threshold(0.7) == pytest.approx(0.75)
    assert calibration.raised_threshold(0.93) == calibration.MAX_TUNED_THRESHOLD
    assert calibration.raised_threshold(0.95) == calibration.MAX_TUNED_THRESHOLD


def test_correct_alerts_lower_slowly_and_stop_at_the_floor():
    assert calibration.lowered_threshold(0.7) == pytest.approx(0.69)
    value = 0.7
    for _ in range(100):
        value = calibration.lowered_threshold(value)
    assert value == calibration.MIN_TUNED_THRESHOLD


def test_a_threshold_set_below_the_floor_is_left_alone():
    assert calibration.lowered_threshold(0.5) == 0.5
    assert calibration.lowered_threshold(calibration.MIN_TUNED_THRESHOLD) == 0.6


def test_one_false_alarm_outweighs_several_confirmations():
    assert calibration.FALSE_ALARM_STEP >= 4 * calibration.CORRECT_STEP
