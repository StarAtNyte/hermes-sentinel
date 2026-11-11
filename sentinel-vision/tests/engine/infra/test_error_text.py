"""Short error text masks URL credentials."""

from __future__ import annotations

from harness.plugin_loader import plugin_module

infra_mod = plugin_module("sentinel.engine.infra")
judge_mod = plugin_module("sentinel.vision")


def test_short_error_redacts_urls():
    text = infra_mod.short_error(RuntimeError("cannot open rtsp://bob:secret@cam/stream now"))
    assert "secret" not in text and "now" in text
    assert len(infra_mod.short_error("x" * 1000)) == infra_mod.MAX_ERROR_TEXT
