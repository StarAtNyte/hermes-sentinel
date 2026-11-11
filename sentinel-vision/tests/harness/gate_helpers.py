"""Shared helpers for the change-gate tests (scene runner and precision/recall scoring)."""

from __future__ import annotations

import math

from harness.plugin_loader import plugin_module
from harness.scenes import Scene

gate_mod = plugin_module("sentinel.vision.gate")
ChangeGate = gate_mod.ChangeGate
GateConfig = gate_mod.GateConfig
bbox_iou = gate_mod.bbox_iou

DETECT_WITHIN = 3
MIN_IOU = 0.3
SEEDS = (0, 1, 2)
BACKENDS = ["numpy"] + (["cv2"] if gate_mod.cv2_available() else [])


def run_scene(scene: Scene, gate: ChangeGate | None = None, **cfg) -> list:
    gate = gate or ChangeGate(GateConfig(**cfg), backend="numpy")
    return [gate.process(f, i / scene.fps) for i, f in enumerate(scene.frames)]


def event_bbox(scene: Scene, index: int):
    # shake_with_mug labels are in content coordinates; the object bbox is the same box.
    return scene.meta.get("object_bbox") or scene.labels[index].bbox


def evaluate(scene: Scene, results: list) -> dict:
    """Precision/recall of changed frames against labelled events (onset + DETECT_WITHIN)."""
    changed = [i for i, r in enumerate(results) if r.changed]
    events = scene.change_indices
    matched_frames: set[int] = set()
    detected = 0
    ious: list[float] = []
    for ev in events:
        hits = [i for i in changed if ev <= i <= ev + DETECT_WITHIN]
        if not hits:
            continue
        best = max(bbox_iou(results[i].bbox, event_bbox(scene, ev)) for i in hits)
        ious.append(best)
        if best >= MIN_IOU:
            detected += 1
            matched_frames.update(hits)
    tp = len(matched_frames)
    fp = len(changed) - tp
    precision = tp / len(changed) if changed else 1.0
    recall = detected / len(events) if events else 1.0
    return {
        "changed": changed,
        "events": events,
        "fp": fp,
        "precision": precision,
        "recall": recall,
        "min_iou": min(ious) if ious else math.nan,
    }
