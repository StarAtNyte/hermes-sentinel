"""Scene-level accuracy sweep of the change gate over every harness scenario."""

from __future__ import annotations

import math

import numpy as np
import pytest
from harness.gate_helpers import BACKENDS, MIN_IOU, SEEDS, evaluate, run_scene
from harness.plugin_loader import plugin_module
from harness.scenes import (
    EVENT_SCENARIOS,
    NUISANCE_SCENARIOS,
    SCENARIOS,
    build_scene,
    object_appears,
)

gate_mod = plugin_module("sentinel.vision.gate")
ops_mod = plugin_module("sentinel.vision.gate.ops")
geometry_mod = plugin_module("sentinel.vision.gate.geometry")
motion_mod = plugin_module("sentinel.vision.gate.motion")
ChangeGate = gate_mod.ChangeGate
GateConfig = gate_mod.GateConfig
GateResult = gate_mod.GateResult
bbox_iou = gate_mod.bbox_iou


_SUMMARY: dict[tuple[str, int, str], dict] = {}


# ------------------------------------------------------------- scene sweep


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.parametrize("seed", SEEDS)
@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_scene_accuracy(name: str, seed: int, backend: str, record_property) -> None:
    scene = build_scene(name, seed=seed)
    gate = ChangeGate(backend=backend)
    results = run_scene(scene, gate)
    m = evaluate(scene, results)
    _SUMMARY[(name, seed, backend)] = m
    record_property("precision", m["precision"])
    record_property("recall", m["recall"])
    record_property("min_iou", m["min_iou"])
    if name in NUISANCE_SCENARIOS or not scene.should_trigger:
        assert m["changed"] == [], f"{name}: nuisance produced changed frames {m['changed']}"
    else:
        assert name in EVENT_SCENARIOS
        assert m["recall"] == 1.0, f"{name}: events {m['events']} changed {m['changed']} {m}"
        assert m["precision"] == 1.0, f"{name}: spurious changed frames {m}"
        assert m["min_iou"] >= MIN_IOU


def test_scene_summary(capsys) -> None:
    """Aggregate precision/recall over every scene (runs the numpy path if not yet swept)."""
    for name in sorted(SCENARIOS):
        for seed in SEEDS:
            key = (name, seed, "numpy")
            if key not in _SUMMARY:
                scene = build_scene(name, seed=seed)
                _SUMMARY[key] = evaluate(scene, run_scene(scene))
    tp = fp = n_events = detected = 0
    lines = [f"{'scene':26s} {'prec':>5s} {'recall':>6s} {'min_iou':>7s} {'fp':>3s}"]
    for name in sorted(SCENARIOS):
        rows = [_SUMMARY[(name, s, "numpy")] for s in SEEDS]
        prec = float(np.mean([r["precision"] for r in rows]))
        rec = float(np.mean([r["recall"] for r in rows]))
        ious = [r["min_iou"] for r in rows if not math.isnan(r["min_iou"])]
        f = sum(r["fp"] for r in rows)
        lines.append(
            f"{name:26s} {prec:5.2f} {rec:6.2f} {min(ious) if ious else float('nan'):7.2f} {f:3d}"
        )
        for r in rows:
            fp += r["fp"]
            tp += len(r["changed"]) - r["fp"]
            n_events += len(r["events"])
            detected += round(r["recall"] * len(r["events"]))
    precision = tp / (tp + fp) if tp + fp else 1.0
    recall = detected / n_events if n_events else 1.0
    lines.append(f"OVERALL precision={precision:.3f} recall={recall:.3f}")
    with capsys.disabled():
        print("\n" + "\n".join(lines))
    assert precision == 1.0
    assert recall == 1.0


@pytest.mark.parametrize("name", ["object_appears", "object_removed"])
def test_persistent_change_fires_once(name: str) -> None:
    scene = build_scene(name, seed=3, n_frames=40)
    changed = [i for i, r in enumerate(run_scene(scene)) if r.changed]
    assert changed == [10]


def test_event_detected_at_larger_resolution() -> None:
    """A 4x upscaled (640x480) scene is downscaled to 320 and bboxes map back to input pixels."""
    scene = object_appears(seed=1)
    big = [np.kron(f, np.ones((4, 4, 1), dtype=np.uint8)) for f in scene.frames]
    gate = ChangeGate(backend="numpy")
    results = [gate.process(f, i) for i, f in enumerate(big)]
    changed = [i for i, r in enumerate(results) if r.changed]
    assert changed == [10]
    x, y, w, h = scene.meta["object_bbox"]
    assert bbox_iou(results[10].bbox, (4 * x, 4 * y, 4 * w, 4 * h)) >= 0.4


def test_low_light_event_still_detected() -> None:
    """The noise model adapts: a mug appearing in a dark, noisy room is still caught."""
    from harness import scenes as S

    for seed in range(4):
        rng = np.random.default_rng(seed)
        bg = S._background(rng, *S.CAMERA_SIZE)
        content, present, box = S._mug_content(bg, 25, 12, appears=True)

        def nuisance(i, img, r):
            dim = img * np.float32(0.22)
            shot = r.normal(0.0, 1.0, size=dim.shape).astype(np.float32)
            return dim + shot * np.sqrt(np.maximum(dim, 0.0)) * np.float32(0.8)

        scene = S._assemble(
            name="lowlight_mug",
            seed=seed,
            fps=10.0,
            content=content,
            present=present,
            rng=rng,
            nuisance=nuisance,
            noise_sigma=11.0,
            should_trigger=True,
            description="",
            meta={"object_bbox": box},
        )
        m = evaluate(scene, run_scene(scene))
        assert m["recall"] == 1.0 and m["precision"] == 1.0, (seed, m)
