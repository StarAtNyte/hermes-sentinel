"""``sentinel_watch`` mentions setup problems only while a check fails."""

from __future__ import annotations

from harness.plugin_loader import plugin_module

cfg_mod = plugin_module("sentinel.config")


CAM = "camera:7"


def test_watch_mentions_setup_only_when_a_check_fails(rig, no_real_device_probe):
    ok = rig.watch(recipe="desk-intruder", source=CAM)
    assert "Setup problem" not in ok["message"]
    assert "/sentinel setup --test-delivery" in ok["message"]
    assert "vision-capable model" in ok["message"]
    rig.sentinel.config = cfg_mod.SentinelConfig(vision_calls_per_hour=0)
    bad = rig.watch(recipe="desk-intruder", source=CAM)
    assert "Setup problem (Vision call caps)" in bad["message"]
    assert "/sentinel setup" in bad["message"]
