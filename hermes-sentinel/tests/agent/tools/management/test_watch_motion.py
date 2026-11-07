"""``sentinel_watch`` motion flag and static recipes."""

from __future__ import annotations

from harness.tool_calls import CAM


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
    assert rig.engine.state.rules[rule_id].spec.motion is False


def test_motion_must_be_a_boolean(rig):
    rule_id = rig.watch("tell me if there is a package", source=CAM)["rule"]["rule_id"]
    bad = rig.call("sentinel_update", rule_id=rule_id, action="edit", changes={"motion": "yes"})
    assert bad["ok"] is False and "true or false" in bad["error"]


def test_recipes_stay_static(rig):
    assert rig.watch(recipe="desk-intruder", source=CAM)["rule"]["motion"] is False
