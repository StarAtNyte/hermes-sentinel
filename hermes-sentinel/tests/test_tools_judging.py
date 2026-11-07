"""Tool-level pieces of the judging work: the motion flag and feedback calibration."""

from __future__ import annotations

import cv2
import numpy as np
import pytest
from harness.fake_sources import FakeSource
from harness.scenes import build_scene
from sentinel_rig import Rig, verdict

CAM = "camera:7"


@pytest.fixture
def rig(fake_ctx):
    r = Rig(fake_ctx)
    r.sources[CAM] = FakeSource(build_scene("static_scene", n_frames=20), r.clock, loop=True)
    fake_ctx.llm.respond(lambda call: verdict(False, 0.9, "empty"), task="sentinel_vision")
    return r


def fire_event(rig, rule_id):
    jpeg = cv2.imencode(".jpg", np.zeros((20, 20, 3), np.uint8))[1].tobytes()
    path = rig.sentinel.store.save_snapshot(jpeg, rule_id=rule_id)
    return rig.sentinel.store.add_event(rule_id, caption="x", snapshot_path=path)


def test_watch_detects_a_motion_question_without_the_llm(rig):
    out = rig.watch("tell me if someone is walking toward the door", source=CAM)
    assert out["rule"]["motion"] is True
    still = rig.watch("tell me if there is a package", source=CAM)
    assert still["rule"]["motion"] is False


def test_watch_motion_argument_overrides_and_edit_changes_it(rig):
    out = rig.watch("tell me if there is a package", source=CAM, motion=True)
    rule_id = out["rule"]["rule_id"]
    assert out["rule"]["motion"] is True
    edited = rig.call("sentinel_update", rule_id=rule_id, action="edit", changes={"motion": False})
    assert edited["ok"] and edited["rule"]["motion"] is False
    assert rig.engine._rules[rule_id].spec.motion is False


def test_motion_must_be_a_boolean(rig):
    rule_id = rig.watch("tell me if there is a package", source=CAM)["rule"]["rule_id"]
    bad = rig.call("sentinel_update", rule_id=rule_id, action="edit", changes={"motion": "yes"})
    assert bad["ok"] is False and "true or false" in bad["error"]


def test_recipes_stay_static(rig):
    assert rig.watch(recipe="desk-intruder", source=CAM)["rule"]["motion"] is False


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
    assert rig.engine._rules[rule_id].spec.confidence_threshold == pytest.approx(0.6)


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
