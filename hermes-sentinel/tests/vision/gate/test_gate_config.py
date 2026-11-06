"""GateConfig validation and mapping tests."""

from __future__ import annotations

import pytest
from harness.plugin_loader import plugin_module

gate_mod = plugin_module("sentinel.vision.gate")
ops_mod = plugin_module("sentinel.vision.gate.ops")
geometry_mod = plugin_module("sentinel.vision.gate.geometry")
motion_mod = plugin_module("sentinel.vision.gate.motion")
ChangeGate = gate_mod.ChangeGate
GateConfig = gate_mod.GateConfig
GateResult = gate_mod.GateResult
bbox_iou = gate_mod.bbox_iou


# ----------------------------------------------------------------- config


def test_config_validation() -> None:
    for bad in [
        {"window": 4},
        {"window": 1},
        {"max_width": 8},
        {"background_alpha": 0},
        {"global_change_max": 0},
        {"min_area_ratio": 1.0},
        {"noise_k": 0},
        {"shift_tolerance": 9},
        {"idle_check_seconds": float("inf")},
        {"noise_bins": 0},
        {"min_dissimilarity": 1.0},
        {"blur_sigma": -1},
    ]:
        with pytest.raises(ValueError):
            GateConfig(**bad)


def test_config_from_mapping() -> None:
    cfg = GateConfig.from_mapping(
        {
            "idle_check_seconds": 60,
            "window": 9.0,
            "tick_on_start": False,
            "unknown": 1,
            "min_score": None,
        }
    )
    assert cfg.idle_check_seconds == 60.0 and cfg.window == 9 and cfg.tick_on_start is False
    assert cfg.min_score == GateConfig().min_score
    assert GateConfig.from_mapping(None) == GateConfig()
    for bad in [
        {"window": 7.5},
        {"window": "x"},
        {"tick_on_start": "yes"},
        {"min_score": True},
        {"window": True},
    ]:
        with pytest.raises(ValueError):
            GateConfig.from_mapping(bad)
