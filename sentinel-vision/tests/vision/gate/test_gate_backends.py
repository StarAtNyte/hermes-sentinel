"""Backend parity and selection tests for the change gate."""

from __future__ import annotations

import numpy as np
import pytest
from harness.gate_helpers import run_scene
from harness.plugin_loader import plugin_module
from harness.scenes import (
    build_scene,
)

gate_mod = plugin_module("sentinel.vision.gate")
ops_mod = plugin_module("sentinel.vision.gate.ops")
geometry_mod = plugin_module("sentinel.vision.gate.geometry")
motion_mod = plugin_module("sentinel.vision.gate.motion")
ChangeGate = gate_mod.ChangeGate
GateConfig = gate_mod.GateConfig
GateResult = gate_mod.GateResult
bbox_iou = gate_mod.bbox_iou


# ------------------------------------------------------------- backends


@pytest.mark.skipif(not gate_mod.cv2_available(), reason="OpenCV not installed")
def test_backend_ops_parity() -> None:
    rng = np.random.default_rng(0)
    img = rng.uniform(0, 255, size=(57, 83)).astype(np.float32)
    np_ops, cv_ops = ops_mod.ImageOps(use_cv2=False), ops_mod.ImageOps(use_cv2=True)
    assert np.allclose(np_ops.box_mean(img, 7), cv_ops.box_mean(img, 7), atol=1e-2)
    assert np.allclose(np_ops.blur(img, 1.5), cv_ops.blur(img, 1.5), atol=1e-2)
    big = rng.uniform(0, 255, size=(240, 640)).astype(np.float32)
    assert np.allclose(np_ops.downscale(big, 320), cv_ops.downscale(big, 320), atol=1e-2)
    odd = rng.uniform(0, 255, size=(301, 997)).astype(np.float32)
    a, b = np_ops.downscale(odd, 320), cv_ops.downscale(odd, 320)
    assert a.shape == b.shape == (97, 320)
    assert np.abs(a - b).mean() < 1.0
    mask = rng.uniform(size=(40, 50)) > 0.6
    assert np.array_equal(np_ops.open(mask, 1), cv_ops.open(mask, 1))
    assert np.array_equal(np_ops.open(mask, 2), cv_ops.open(mask, 2))


@pytest.mark.skipif(not gate_mod.cv2_available(), reason="OpenCV not installed")
def test_backend_results_match() -> None:
    scene = build_scene("shake_with_mug", seed=1)
    a = run_scene(scene, ChangeGate(backend="numpy"))
    b = run_scene(scene, ChangeGate(backend="cv2"))
    assert [r.changed for r in a] == [r.changed for r in b]
    for ra, rb in zip(a, b, strict=True):
        if ra.changed:
            assert bbox_iou(ra.bbox, rb.bbox) > 0.8


def test_backend_selection() -> None:
    assert ChangeGate(backend="numpy").backend == "numpy"
    with pytest.raises(ValueError):
        ChangeGate(backend="gpu")  # type: ignore[arg-type]
    if gate_mod.cv2_available():
        assert ChangeGate(backend="cv2").backend == "cv2"
        assert ChangeGate().backend == "cv2"


def test_cv2_backend_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ops_mod, "_cv2", None)
    with pytest.raises(RuntimeError):
        ChangeGate(backend="cv2")
    gate = ChangeGate(backend="auto")
    assert gate.backend == "numpy"
    assert not gate_mod.cv2_available()
