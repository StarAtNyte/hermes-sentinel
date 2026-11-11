"""``sentinel_rules``."""

from __future__ import annotations

from harness.tool_calls import watch


def test_rules_lists_status(rig):
    rule_id = watch(rig)["rule"]["rule_id"]
    rig.pump(3)
    out = rig.call("sentinel_rules")
    assert out["ok"] and out["count"] == 1
    entry = out["rules"][0]
    assert entry["rule_id"] == rule_id and entry["status"] == "idle"
    assert entry["vision_checks"]["this_hour"] == 1  # the start-up tick
    assert entry["checks_since_load"] == 1 and entry["last_event"] is None
    assert "Vision checks" in out["budget"]
    assert rig.call("sentinel_rules", rule_id="zzz")["ok"] is False
