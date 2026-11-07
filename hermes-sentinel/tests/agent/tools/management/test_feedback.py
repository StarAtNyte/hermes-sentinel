"""``sentinel_feedback``."""

from __future__ import annotations

from harness.tool_calls import fire_event, watch


def test_feedback_correct_adds_positive_example(rig):
    rule_id = watch(rig)["rule"]["rule_id"]
    ev = fire_event(rig, rule_id)
    out = rig.call("sentinel_feedback", event_id=ev.id, verdict="correct")
    assert out["ok"] and out["example_id"] and "'yes' example" in out["message"]
    assert len(rig.sentinel.store.list_examples(rule_id, "pos")) == 1


def test_false_alarm_threshold_is_capped(rig):
    rule_id = watch(rig)["rule"]["rule_id"]
    for _ in range(8):
        ev = fire_event(rig, rule_id)
        out = rig.call("sentinel_feedback", event_id=f"#{ev.id}", verdict="false alarm")
        assert out["ok"], out
    assert out["confidence_threshold"] == 0.95
    assert rig.engine.state.rules[rule_id].spec.confidence_threshold == 0.95


def test_feedback_errors_and_deleted_rule(rig):
    rule_id = watch(rig)["rule"]["rule_id"]
    assert "no event #99" in rig.call("sentinel_feedback", event_id=99, verdict="correct")["error"]
    assert rig.call("sentinel_feedback", event_id=1, verdict="meh")["ok"] is False
    assert rig.call("sentinel_feedback", event_id=1.5, verdict="correct")["ok"] is False
    ev = rig.sentinel.store.add_event(rule_id, caption="no photo")
    out = rig.call("sentinel_feedback", event_id=ev.id, verdict="false_alarm")
    assert out["ok"] and "example_id" not in out
    rig.call("sentinel_update", rule_id=rule_id, action="delete")
    out = rig.call("sentinel_feedback", event_id=ev.id, verdict="correct")
    assert out["ok"] and "no longer exists" in out["message"]
