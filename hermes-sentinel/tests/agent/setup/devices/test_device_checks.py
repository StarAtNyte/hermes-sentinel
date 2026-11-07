"""Setup checks about cameras, screens and the default source."""

from __future__ import annotations

import pytest
from harness.plugin_loader import plugin_module
from harness.setup_rig import info, run

cfg_mod = plugin_module("sentinel.config")


def test_no_camera_found(tmp_path):
    c = run(tmp_path, list_sources=lambda **kw: [])["sources"]
    assert c.status == "warn" and "No camera" in c.detail
    off = run(tmp_path, list_sources=lambda **kw: [info("camera:0", available=False)])["sources"]
    assert off.status == "warn"


def test_sources_found_configured_and_not_probed(tmp_path):
    found = run(tmp_path)["sources"]
    assert found.status == "ok" and "camera:0" in found.detail and "screen:1" in found.detail
    stream = run(tmp_path, list_sources=lambda **kw: [info("porch", "stream", None, "porch")])[
        "sources"
    ]
    assert stream.status == "ok" and "porch (stream)" in stream.detail

    def boom(**kw):
        raise AssertionError("must not probe")

    skipped = run(tmp_path, probe_sources=False, list_sources=boom)["sources"]
    assert skipped.status == "ok" and "not probed" in skipped.detail

    def crash(**kw):
        raise RuntimeError("driver")

    crashed = run(tmp_path, list_sources=crash)["sources"]
    assert crashed.status == "warn" and "RuntimeError" in crashed.detail


def test_probe_uses_short_timeouts(tmp_path):
    seen = {}

    def probe(**kw):
        seen.update(kw)
        return []

    run(tmp_path, list_sources=probe)
    assert seen["total_timeout"] <= 5 and seen["per_camera_timeout"] <= 2


@pytest.mark.parametrize(
    "source, status",
    [
        ("camera:0", "ok"),
        ("screen", "ok"),
        ("rtsp://user:pw@h/x", "fail"),
        ("nonsense://x", "fail"),
    ],
)
def test_default_source_validity(tmp_path, source, status):
    cfg = cfg_mod.SentinelConfig(default_source=source)
    c = run(tmp_path, config=cfg, list_sources=lambda **kw: [])["default_source"]
    assert c.status == status, c.detail
    if status == "fail":
        assert "pw" not in c.detail  # credentials never echoed


def test_default_source_not_among_found_devices(tmp_path):
    cfg = cfg_mod.SentinelConfig(default_source="camera:3")
    c = run(tmp_path, config=cfg)["default_source"]
    assert c.status == "warn" and "camera:0" in c.detail
