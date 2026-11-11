"""``sentinel_update``: pause, resume, edit, delete and its errors."""

from __future__ import annotations

import pytest
from harness.tool_calls import CAM, watch


def test_update_pause_resume_edit_delete(rig):
    rule_id = watch(rig)["rule"]["rule_id"]
    out = rig.call("sentinel_update", rule_id=rule_id, action="pause")
    assert out["ok"] and out["rule"]["enabled"] is False
    assert rig.call("sentinel_rules")["rules"][0]["status"] == "paused"
    assert rig.engine.source_keys() == []
    out = rig.call("sentinel_update", rule_id=rule_id, action="resume")
    assert out["rule"]["enabled"] is True and rig.engine.source_keys() == [CAM]
    out = rig.call(
        "sentinel_update",
        rule_id=rule_id,
        action="edit",
        changes={
            "question": "Is anyone at the desk?",
            "mode": "condition",
            "hold_seconds": 30,
            "confidence_threshold": 0.8,
            "roi": None,
            "action_prompt": "say hi",
            "schedule": None,
            "cooldown_seconds": 10,
        },
    )
    assert out["ok"], out
    rule = out["rule"]
    assert rule["question"] == "Is anyone at the desk?" and rule["mode"] == "condition"
    assert rule["action"] == "agent" and rule["confidence_threshold"] == 0.8
    out = rig.call("sentinel_update", rule_id=rule_id, action="edit", changes={"action_prompt": ""})
    assert out["rule"]["action"] == "notify"
    out = rig.call("sentinel_update", rule_id=rule_id, action="delete")
    assert out["ok"] and "events are kept" in out["message"]
    assert rig.sentinel.store.get_rule(rule_id) is None and rig.engine.rule_ids() == []


@pytest.mark.parametrize(
    ("args", "needle"),
    [
        ({"action": "pause"}, "'rule_id' is required"),
        ({"rule_id": "nope", "action": "pause"}, "no rule with id"),
        ({"action": "explode"}, "action must be"),
        ({"action": "edit"}, "non-empty 'changes'"),
        ({"action": "edit", "changes": {"enabled": False}}, "cannot change"),
        ({"action": "edit", "changes": {"mode": "sometimes"}}, "mode must be"),
        ({"action": "edit", "changes": {"confidence_threshold": 3}}, "invalid rule"),
        ({"action": "edit", "changes": {"source": "nonsense:::"}}, "invalid source"),
    ],
)
def test_update_errors(rig, args, needle):
    rule_id = watch(rig)["rule"]["rule_id"]
    args.setdefault("rule_id", rule_id)
    if args.get("rule_id") is None or "'rule_id' is required" in needle:
        args.pop("rule_id")
    out = rig.call("sentinel_update", **args)
    assert out["ok"] is False and needle in out["error"], out
