"""``sentinel_feedback`` threshold calibration."""

from __future__ import annotations

import pytest
from harness.tool_calls import CAM, fire_event


def test_correct_confirmations_lower_the_threshold_slowly_and_stop_at_the_floor(rig):
    rule_id = rig.watch(recipe="desk-intruder", source=CAM)["rule"]["rule_id"]
    seen = []
    for _ in range(14):
        ev = fire_event(rig, rule_id)
        out = rig.call("sentinel_feedback", event_id=ev.id, verdict="correct")
        assert out["ok"], out
        seen.append(out["confidence_threshold"])
    assert seen[0] == pytest.approx(0.69) and seen[1] == pytest.approx(0.68)
    assert min(seen) == pytest.approx(0.6) and seen[-1] == pytest.approx(0.6)
    assert rig.engine.state.rules[rule_id].spec.confidence_threshold == pytest.approx(0.6)


def test_repeating_the_same_feedback_does_not_move_the_threshold_again(rig):
    rule_id = rig.watch(recipe="desk-intruder", source=CAM)["rule"]["rule_id"]
    ev = fire_event(rig, rule_id)
    first = rig.call("sentinel_feedback", event_id=ev.id, verdict="correct")
    again = rig.call("sentinel_feedback", event_id=ev.id, verdict="correct")
    assert again["already_recorded"] is True
    assert again["confidence_threshold"] == first["confidence_threshold"]


def test_a_threshold_set_below_the_floor_by_hand_is_not_touched(rig):
    rule_id = rig.watch(recipe="desk-intruder", source=CAM)["rule"]["rule_id"]
    rig.call(
        "sentinel_update", rule_id=rule_id, action="edit", changes={"confidence_threshold": 0.5}
    )
    out = rig.call("sentinel_feedback", event_id=fire_event(rig, rule_id).id, verdict="correct")
    assert out["confidence_threshold"] == pytest.approx(0.5)
