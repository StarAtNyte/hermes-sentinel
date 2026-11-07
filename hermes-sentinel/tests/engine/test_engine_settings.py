"""EngineSettings validation."""

from __future__ import annotations

import pytest
from harness.plugin_loader import plugin_module

engine_mod = plugin_module("sentinel.engine")
judge_mod = plugin_module("sentinel.vision")


def test_settings_validation():
    with pytest.raises(ValueError):
        engine_mod.EngineSettings(capture_interval=0)
    with pytest.raises(ValueError):
        engine_mod.EngineSettings(queue_size=0)
    with pytest.raises(ValueError):
        engine_mod.EngineSettings(workers=99)
    with pytest.raises(ValueError):
        engine_mod.EngineSettings(offline_grace=-1)
