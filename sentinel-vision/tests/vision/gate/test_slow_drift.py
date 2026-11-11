"""Slow-drift detection: gradual real events trigger, lighting and shake nuisances do not."""

from __future__ import annotations

import numpy as np
import pytest
from harness.drift_scenes import DRIFT_SCENARIOS, build_drift_scene
from harness.gate_helpers import BACKENDS, SEEDS, run_scene
from harness.plugin_loader import plugin_module
from harness.scenes import NUISANCE_SCENARIOS, build_scene

gate_mod = plugin_module("sentinel.vision.gate")
ChangeGate = gate_mod.ChangeGate
GateConfig = gate_mod.GateConfig

GRACE_FRAMES = 12
MID_SPAN = (60, 140)


def _contained(inner, outer) -> float:
    """Fraction of the ``inner`` box that lies inside ``outer``."""
    ix = max(0, min(inner[0] + inner[2], outer[0] + outer[2]) - max(inner[0], outer[0]))
    iy = max(0, min(inner[1] + inner[3], outer[1] + outer[3]) - max(inner[1], outer[1]))
    return ix * iy / (inner[2] * inner[3])


def _padded(box, pad: int = 4):
    """``box`` grown by the blur halo the gate's filters add around real edges."""
    return (box[0] - pad, box[1] - pad, box[2] + 2 * pad, box[3] + 2 * pad)


@pytest.mark.parametrize(
    ("backend", "seed"),
    [("numpy", seed) for seed in SEEDS] + [(b, 0) for b in BACKENDS if b != "numpy"],
)
@pytest.mark.parametrize("name", sorted(DRIFT_SCENARIOS))
def test_gradual_event_triggers_drift(name: str, seed: int, backend: str) -> None:
    scene = build_drift_scene(name, seed=seed)
    start, end, final = scene.meta["start"], scene.meta["end"], scene.meta["final_bbox"]
    results = run_scene(scene, ChangeGate(backend=backend))
    triggers = [i for i, r in enumerate(results) if r.changed]
    drifts = [i for i, r in enumerate(results) if r.drift]
    assert triggers and min(triggers) >= start, f"{name}: early or missing trigger {triggers}"
    assert max(triggers) <= end + GRACE_FRAMES, f"{name}: late trigger {triggers}"
    assert len(drifts) >= 2, f"{name}: drift fired {drifts}"
    assert all(results[i].reason == "drift" and results[i].should_judge for i in drifts)
    assert all(_contained(results[i].bbox, _padded(final)) >= 0.8 for i in drifts)


@pytest.mark.parametrize("name", sorted(DRIFT_SCENARIOS))
def test_fast_path_alone_misses_the_middle_of_the_change(name: str) -> None:
    scene = build_drift_scene(name, seed=0)
    off = run_scene(scene, ChangeGate(GateConfig(drift_enabled=False), backend="numpy"))
    on = run_scene(scene, ChangeGate(backend="numpy"))
    lo, hi = MID_SPAN
    assert not any(r.changed for r in off[lo:hi])
    assert any(r.drift for r in on[lo:hi])
    assert not any(r.drift for r in off)


@pytest.mark.parametrize("seed", SEEDS)
@pytest.mark.parametrize("name", NUISANCE_SCENARIOS)
def test_nuisance_scenes_produce_no_drift(name: str, seed: int) -> None:
    results = run_scene(build_scene(name, seed=seed))
    assert [i for i, r in enumerate(results) if r.drift] == []


def test_precision_and_recall_over_all_scenes() -> None:
    """Every trigger in every drift scene falls inside the labelled event window."""
    spurious = missed = 0
    for name in DRIFT_SCENARIOS:
        for seed in SEEDS:
            scene = build_drift_scene(name, seed=seed)
            start, end = scene.meta["start"], scene.meta["end"]
            hits = [i for i, r in enumerate(run_scene(scene)) if r.changed]
            spurious += sum(not start <= i <= end + GRACE_FRAMES for i in hits)
            missed += int(not hits)
    assert spurious == 0
    assert missed == 0


def test_drift_counts_in_stats() -> None:
    scene = build_drift_scene("liquid_rising", seed=0)
    gate = ChangeGate(backend="numpy")
    results = run_scene(scene, gate)
    assert gate.stats()["drifts"] == sum(r.drift for r in results) > 0


def test_drift_refreshes_after_firing_so_it_does_not_repeat_on_a_static_scene() -> None:
    scene = build_drift_scene("door_opening", seed=1)
    results = run_scene(scene)
    assert not any(r.changed for r in results[scene.meta["end"] + GRACE_FRAMES :])


def test_change_outside_roi_is_not_drift() -> None:
    scene = build_drift_scene("liquid_rising", seed=0)
    gate = ChangeGate(backend="numpy", roi=(0.6, 0.0, 0.4, 1.0))  # glass sits at the left
    assert not any(r.changed for r in run_scene(scene, gate)[10:])


def test_tick_makes_the_judged_frame_the_slow_reference() -> None:
    gate = ChangeGate(GateConfig(idle_check_seconds=5.0, tick_on_start=False), backend="numpy")
    scene = build_scene("static_scene", seed=0, n_frames=80)
    ticks = [i for i, r in enumerate(run_scene(scene, gate)) if r.tick]
    assert ticks == [50]
    assert gate._state.slow_ts == pytest.approx(5.0)


def test_drift_checks_are_spaced() -> None:
    gate = ChangeGate(GateConfig(drift_check_seconds=2.0), backend="numpy")
    scene = build_scene("static_scene", seed=0, n_frames=60)
    run_scene(scene, gate)
    assert gate._state.drift_checked is not None
    assert gate._state.drift_checked <= 5.9


def test_reset_clears_slow_reference() -> None:
    gate = ChangeGate(backend="numpy")
    run_scene(build_scene("static_scene", seed=0, n_frames=5), gate)
    assert gate._state.slow_ref is not None
    gate.reset()
    assert gate._state.slow_ref is None
    assert gate._state.drift_streak == 0


def test_global_brightness_step_does_not_drift() -> None:
    scene = build_scene("static_scene", seed=2, n_frames=80)
    frames = [
        np.clip(f.astype(np.float32) * (1.0 if i < 40 else 1.5), 0, 255).astype(np.uint8)
        for i, f in enumerate(scene.frames)
    ]
    gate = ChangeGate(backend="numpy")
    results = [gate.process(f, i / 10.0) for i, f in enumerate(frames)]
    assert not any(r.drift for r in results)


@pytest.mark.parametrize(
    "bad",
    [
        {"drift_threshold_factor": 0.0},
        {"drift_threshold_factor": 1.5},
        {"drift_max_area_ratio": 0.0},
        {"drift_min_fill": 2.0},
        {"drift_confirm_checks": 0},
        {"drift_refresh_seconds": 0.0},
        {"drift_check_seconds": -1.0},
        {"drift_min_area_ratio": 1.0},
    ],
)
def test_drift_config_validation(bad: dict) -> None:
    with pytest.raises(ValueError):
        GateConfig(**bad)


def test_drift_config_from_mapping() -> None:
    cfg = GateConfig.from_mapping({"drift_enabled": False, "drift_confirm_checks": 3})
    assert cfg.drift_enabled is False and cfg.drift_confirm_checks == 3
    with pytest.raises(ValueError):
        GateConfig.from_mapping({"drift_enabled": "yes"})
