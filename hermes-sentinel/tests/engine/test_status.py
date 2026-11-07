"""Status dicts."""

from __future__ import annotations

from harness.plugin_loader import plugin_module

alerts = plugin_module("sentinel.alerts")
judge_mod = plugin_module("sentinel.vision")
rules = plugin_module("sentinel.rules")


def test_status_shape(h):
    h.add()
    h.engine.step_source("cam")
    st = h.engine.status()
    assert st["rules"] == 1 and st["active_rules"] == 1 and st["queue"] == 0
    assert st["sources"][0]["source"] == "cam" and st["sources"][0]["frames"] >= 1
    assert set(st["alerts"]) == {"sent", "suppressed", "failed"}
    rs = h.engine.rule_status("r1")
    assert rs["state"] == "idle" and rs["source_online"] is True
    assert h.engine.rule_status("nope") is None
