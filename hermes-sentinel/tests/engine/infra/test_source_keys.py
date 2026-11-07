"""Grouping keys for rule sources."""

from __future__ import annotations

from harness.plugin_loader import plugin_module

engine_mod = plugin_module("sentinel.engine")
judge_mod = plugin_module("sentinel.vision")


def test_default_source_key():
    assert engine_mod.default_source_key("webcam:0") == "camera:0"
    assert engine_mod.default_source_key(" whatever?? ") == "whatever??"
    assert engine_mod.default_source_key("front", {"front": "rtsp://u@h/s"}).startswith("stream:")
