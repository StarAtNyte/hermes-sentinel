"""Input blocks: the plain-dict fallback outside Hermes is logged, not silent."""

from __future__ import annotations

import logging
import sys

from harness.plugin_loader import plugin_module

blocks = plugin_module("sentinel.vision.judge.blocks")


def test_image_block_fallback_logs_when_host_type_is_missing(monkeypatch, caplog):
    monkeypatch.setitem(sys.modules, "agent.plugin_llm", None)  # makes the import raise ImportError
    with caplog.at_level(logging.DEBUG, logger=blocks.logger.name):
        block = blocks.image_block(b"\xff\xd8\xff", "image/jpeg", "frame.jpg")
    assert block == {
        "type": "image",
        "data": b"\xff\xd8\xff",
        "mime_type": "image/jpeg",
        "file_name": "frame.jpg",
    }
    assert any("PluginLlmImageInput unavailable" in r.getMessage() for r in caplog.records)
