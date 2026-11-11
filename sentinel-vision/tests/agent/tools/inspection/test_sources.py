"""``sentinel_sources``."""

from __future__ import annotations

from harness.plugin_loader import plugin_module

sources_mod = plugin_module("sentinel.sources")


def test_sentinel_sources(rig, monkeypatch):
    seen = {}

    def fake_list(**kw):
        seen.update(kw)
        return [
            sources_mod.SourceInfo(spec="camera:0", kind="camera", name="Webcam 0", available=True)
        ]

    monkeypatch.setattr(
        plugin_module("sentinel.agent.tools.inspection.sources"), "list_sources", fake_list
    )
    out = rig.call("sentinel_sources", probe_cameras=False)
    assert out["ok"] and out["sources"][0]["spec"] == "camera:0"
    assert seen["probe_cameras"] is False and out["default_source"] == "camera:0"
