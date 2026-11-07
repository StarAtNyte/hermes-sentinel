"""The ``sentinel_setup_check`` tool over a live Sentinel."""

from __future__ import annotations

from harness.plugin_loader import plugin_module


def test_tool_setup_check(rig, no_real_device_probe):
    out = rig.call("sentinel_setup_check")
    assert out["ok"] is True and out["ready"] is True
    assert [c["id"] for c in out["checks"]][:2] == ["deps", "injection"]
    assert out["report"].startswith("Sentinel setup check")
    schema = plugin_module("sentinel.agent.tools").SCHEMAS["sentinel_setup_check"]
    assert set(schema["parameters"]["properties"]) == {"test_delivery"}
