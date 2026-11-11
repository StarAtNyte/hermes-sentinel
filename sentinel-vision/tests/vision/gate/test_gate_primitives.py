"""Gate primitives: ROI geometry, shift estimation, noise levels, results."""

from __future__ import annotations

import numpy as np
import pytest
from harness.plugin_loader import plugin_module
from harness.scenes import (
    static_scene,
)

gate_mod = plugin_module("sentinel.vision.gate")
ops_mod = plugin_module("sentinel.vision.gate.ops")
geometry_mod = plugin_module("sentinel.vision.gate.geometry")
motion_mod = plugin_module("sentinel.vision.gate.motion")
ChangeGate = gate_mod.ChangeGate
GateConfig = gate_mod.GateConfig
GateResult = gate_mod.GateResult
bbox_iou = gate_mod.bbox_iou


@pytest.mark.parametrize(
    "roi,expected",
    [
        (None, None),
        ((0.1, 0.2, 0.3, 0.4), (0.1, 0.2, 0.3, 0.4)),
        ((-0.5, 0.5, 1.0, 1.0), (0.0, 0.5, 0.5, 0.5)),
        ([0, 0, 1, 1], (0.0, 0.0, 1.0, 1.0)),
    ],
)
def test_normalize_roi(roi, expected) -> None:
    got = geometry_mod.normalize_roi(roi)
    if expected is None:
        assert got is None
    else:
        assert np.allclose(got, expected)


@pytest.mark.parametrize(
    "roi",
    [
        (0.1, 0.2, 0.3),
        (0, 0, 0, 0.5),
        (0, 0, 0.5, -1),
        (1.5, 0, 0.2, 0.2),
        ("a", 0, 1, 1),
        (float("nan"), 0, 1, 1),
        5,
    ],
)
def test_normalize_roi_rejects(roi) -> None:
    with pytest.raises(ValueError):
        geometry_mod.normalize_roi(roi)


def test_roi_from_pixels() -> None:
    assert np.allclose(
        geometry_mod.roi_from_pixels((16, 12, 80, 60), (160, 120)), (0.1, 0.1, 0.5, 0.5)
    )
    with pytest.raises(ValueError):
        geometry_mod.roi_from_pixels((0, 0, 1, 1), (0, 10))


def test_tiny_roi_still_valid() -> None:
    rows, cols = geometry_mod.roi_slices((0.999, 0.999, 0.001, 0.001), 100, 50)
    assert rows.stop - rows.start >= 1 and cols.stop - cols.start >= 1


# ------------------------------------------------------------ primitives


@pytest.mark.parametrize("shift", [(0, 0), (2, -1), (-3, 4), (5, 0)])
def test_estimate_shift(shift) -> None:
    ref = static_scene(seed=1, n_frames=1).frames[0].mean(axis=2).astype(np.float32)
    cur = motion_mod.shifted(ref, *shift)
    assert motion_mod.estimate_shift(ref, cur, 8) == shift


def test_estimate_shift_rejects_large_and_flat() -> None:
    ref = static_scene(seed=1, n_frames=1).frames[0].mean(axis=2).astype(np.float32)
    assert motion_mod.estimate_shift(ref, motion_mod.shifted(ref, 6, 0), 4) == (0, 0)
    flat = np.full((40, 40), 7.0, np.float32)
    assert motion_mod.estimate_shift(flat, flat, 8) == (0, 0)
    assert motion_mod.estimate_shift(ref, ref, 0) == (0, 0)


def test_bbox_iou() -> None:
    assert bbox_iou((0, 0, 10, 10), (0, 0, 10, 10)) == 1.0
    assert bbox_iou((0, 0, 10, 10), (5, 0, 10, 10)) == pytest.approx(50 / 150)
    assert bbox_iou((0, 0, 10, 10), (20, 20, 5, 5)) == 0.0
    assert bbox_iou(None, (0, 0, 1, 1)) == 0.0
    assert bbox_iou((0, 0, 0, 0), (0, 0, 0, 0)) == 0.0


def test_noise_level_tracks_brightness() -> None:
    rng = np.random.default_rng(0)
    brightness = np.repeat(np.linspace(0, 255, 64)[None, :], 64, axis=0).astype(np.float32)
    score = (rng.exponential(size=(64, 64)) * (1 + brightness / 10)).astype(np.float32)
    level = motion_mod.noise_level(score, brightness, 8)
    assert level[:, -1].mean() > 5 * level[:, 0].mean()
    flat = motion_mod.noise_level(score, brightness, 1)
    assert np.allclose(flat, np.median(score))


def test_result_should_judge() -> None:
    assert GateResult(True, 0.1, (0, 0, 1, 1)).should_judge
    assert GateResult(False, 0.0, None, tick=True).should_judge
    assert not GateResult(False, 0.0, None).should_judge
