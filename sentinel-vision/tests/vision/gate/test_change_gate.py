"""ChangeGate behaviour: ticks, ROI, baseline handling and robustness."""

from __future__ import annotations

import math
import threading

import numpy as np
import pytest
from harness.gate_helpers import MIN_IOU, run_scene
from harness.plugin_loader import plugin_module
from harness.scenes import (
    build_scene,
    object_appears,
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


# ------------------------------------------------------------------ ticks


def _static_frames(n: int = 5):
    return static_scene(seed=0, n_frames=n).frames


def test_tick_on_start_and_period() -> None:
    gate = ChangeGate(GateConfig(idle_check_seconds=10.0), backend="numpy")
    frames = _static_frames(1) * 30
    ticks = [t for t in range(30) if gate.process(frames[t], float(t)).tick]
    assert ticks == [0, 10, 20]
    assert gate.stats()["ticks"] == 3


def test_no_tick_on_start_when_disabled() -> None:
    gate = ChangeGate(GateConfig(idle_check_seconds=5.0, tick_on_start=False), backend="numpy")
    frame = _static_frames(1)[0]
    ticks = [t for t in range(12) if gate.process(frame, float(t)).tick]
    assert ticks == [5, 10]


def test_ticks_disabled() -> None:
    gate = ChangeGate(GateConfig(idle_check_seconds=0), backend="numpy")
    frame = _static_frames(1)[0]
    assert not any(gate.process(frame, float(t)).tick for t in range(0, 1000, 100))


def test_change_resets_idle_timer() -> None:
    scene = object_appears(seed=0, n_frames=30, appear_at=10)
    gate = ChangeGate(GateConfig(idle_check_seconds=12.0, tick_on_start=False), backend="numpy")
    results = [gate.process(f, float(i)) for i, f in enumerate(scene.frames)]
    assert results[10].changed
    ticks = [i for i, r in enumerate(results) if r.tick]
    assert ticks == [22]  # 12 s after the change at t=10, not at t=12 or t=24
    assert results[22].should_judge and not results[22].changed


def test_clock_backwards_restarts_period() -> None:
    gate = ChangeGate(GateConfig(idle_check_seconds=10.0), backend="numpy")
    frame = _static_frames(1)[0]
    assert gate.process(frame, 100.0).tick
    assert not gate.process(frame, 5.0).tick  # clock reset
    assert not gate.process(frame, 14.0).tick
    assert gate.process(frame, 15.0).tick


def test_default_clock_used_without_ts() -> None:
    now = [50.0]
    gate = ChangeGate(GateConfig(idle_check_seconds=3.0), backend="numpy", clock=lambda: now[0])
    frame = _static_frames(1)[0]
    assert gate.process(frame).tick
    now[0] = 52.0
    assert not gate.process(frame).tick
    now[0] = 53.0
    assert gate.process(frame).tick


def test_non_finite_timestamp_rejected() -> None:
    gate = ChangeGate(backend="numpy")
    with pytest.raises(ValueError):
        gate.process(_static_frames(1)[0], float("nan"))


# -------------------------------------------------------------------- ROI


def test_roi_excluding_change_stays_quiet() -> None:
    scene = object_appears(seed=2)
    # Mug is at x ~ 55-70% of the width; watch only the left 40%.
    gate = ChangeGate(roi=(0.0, 0.0, 0.4, 1.0), backend="numpy")
    assert not any(r.changed for r in run_scene(scene, gate))


def test_roi_including_change_detects_and_clips_bbox() -> None:
    scene = object_appears(seed=2)
    w, h = scene.size
    x, y, bw, bh = scene.meta["object_bbox"]
    roi = geometry_mod.roi_from_pixels((x - 10, y - 10, bw + 20, bh + 20), (w, h))
    gate = ChangeGate(roi=roi, backend="numpy")
    results = run_scene(scene, gate)
    assert [i for i, r in enumerate(results) if r.changed] == [10]
    r = results[10]
    assert bbox_iou(r.bbox, scene.meta["object_bbox"]) >= MIN_IOU
    rx0, ry0 = roi[0] * w, roi[1] * h
    rx1, ry1 = rx0 + roi[2] * w, ry0 + roi[3] * h
    bx, by, bw2, bh2 = r.bbox
    assert bx >= math.floor(rx0) and by >= math.floor(ry0)
    assert bx + bw2 <= math.ceil(rx1) + 1 and by + bh2 <= math.ceil(ry1) + 1
    # Ratio is relative to the (small) ROI, so it is much larger than the full-frame ratio.
    full = run_scene(scene)[10]
    assert r.ratio > 2 * full.ratio


def test_large_roi_change_is_not_mistaken_for_global() -> None:
    """A change filling most of a small ROI must not trigger the global re-baseline."""
    scene = object_appears(seed=0)
    w, h = scene.size
    gate = ChangeGate(
        roi=geometry_mod.roi_from_pixels(scene.meta["object_bbox"], (w, h)), backend="numpy"
    )
    r = run_scene(scene, gate)[10]
    assert r.changed and not r.rebaselined


def test_set_roi_runtime() -> None:
    scene = object_appears(seed=2)
    gate = ChangeGate(roi=(0.0, 0.0, 0.4, 1.0), backend="numpy")
    for i, f in enumerate(scene.frames[:10]):
        gate.process(f, float(i))
    gate.set_roi(None)
    assert gate.roi is None
    assert gate.process(scene.frames[10], 10.0).changed


# --------------------------------------------------- baseline / robustness


def test_first_frame_is_warmup() -> None:
    gate = ChangeGate(backend="numpy")
    r = gate.process(_static_frames(1)[0], 0.0)
    assert not r.changed and r.rebaselined and r.reason == "warmup" and r.bbox is None


def _textured_view(seed: int, n: int) -> list[np.ndarray]:
    """Busy random 6x6-block texture plus sensor noise: a view with structure everywhere."""
    rng = np.random.default_rng(seed)
    blocks = rng.uniform(20, 235, size=(20, 27, 3))
    base = np.kron(blocks, np.ones((6, 6, 1)))[:120, :160]
    return [np.clip(base + rng.normal(0, 2, base.shape), 0, 255).astype(np.uint8) for _ in range(n)]


def test_scene_cut_rebaselines() -> None:
    a = _textured_view(0, 6)
    b = _textured_view(99, 6)  # camera now points at something else entirely
    gate = ChangeGate(backend="numpy")
    results = [gate.process(f, float(i)) for i, f in enumerate(a + b)]
    cut = results[6]
    assert not cut.changed and cut.rebaselined and cut.reason == "global_change"
    assert cut.ratio > gate.config.global_change_max
    assert not any(r.changed for r in results[7:])
    assert gate.stats()["rebaselines"] == 2


def test_resize_rebaselines() -> None:
    gate = ChangeGate(backend="numpy")
    small = static_scene(seed=0, n_frames=3).frames
    big = static_scene(seed=0, n_frames=3, size=(200, 150)).frames
    for i, f in enumerate(small):
        gate.process(f, float(i))
    r = gate.process(big[0], 3.0)
    assert r.rebaselined and r.reason == "resized" and not r.changed
    assert not gate.process(big[1], 4.0).changed


def test_reset_forgets_background_and_ticks_again() -> None:
    gate = ChangeGate(GateConfig(idle_check_seconds=100.0), backend="numpy")
    frame = _static_frames(1)[0]
    gate.process(frame, 0.0)
    gate.process(frame, 1.0)
    gate.reset()
    r = gate.process(frame, 2.0)
    assert r.rebaselined and r.reason == "warmup" and r.tick


def test_update_config() -> None:
    gate = ChangeGate(backend="numpy")
    frame = _static_frames(1)[0]
    gate.process(frame, 0.0)
    gate.update_config(min_area_ratio=0.01)
    assert gate.config.min_area_ratio == 0.01
    assert not gate.process(frame, 1.0).rebaselined  # non-geometry change keeps baseline
    gate.update_config(window=9)
    assert gate.process(frame, 2.0).rebaselined
    with pytest.raises(ValueError):
        gate.update_config(window=4)
    with pytest.raises(TypeError):
        gate.update_config(not_a_field=1)


def test_input_formats_accepted() -> None:
    scene = object_appears(seed=0)
    gray = [
        (0.114 * f[..., 0] + 0.587 * f[..., 1] + 0.299 * f[..., 2]).astype(np.uint8)
        for f in scene.frames
    ]
    variants = {
        "gray": gray,
        "gray1": [g[..., None] for g in gray],
        "bgra": [np.dstack([f, np.full(f.shape[:2], 255, np.uint8)]) for f in scene.frames],
        "float": [f.astype(np.float32) for f in scene.frames],
        "uint16": [f.astype(np.uint16) * 257 for f in scene.frames],
        "noncontig": [np.asfortranarray(f) for f in scene.frames],
    }
    for name, frames in variants.items():
        gate = ChangeGate(backend="numpy")
        changed = [i for i, f in enumerate(frames) if gate.process(f, float(i)).changed]
        assert changed == [10], name


def test_nan_frame_does_not_crash() -> None:
    frames = static_scene(seed=0, n_frames=4).frames
    gate = ChangeGate(backend="numpy")
    for i, f in enumerate(frames):
        fl = f.astype(np.float32)
        if i == 2:
            fl[0, 0] = np.nan
        r = gate.process(fl, float(i))
        assert math.isfinite(r.ratio)


def test_black_frames_do_not_crash() -> None:
    gate = ChangeGate(backend="numpy")
    black = np.zeros((60, 80, 3), np.uint8)
    for i in range(4):
        r = gate.process(black, float(i))
        assert not r.changed and r.ratio == 0.0


@pytest.mark.parametrize(
    "bad",
    [
        None,
        [[1, 2], [3, 4]],
        np.zeros((0, 10, 3), np.uint8),
        np.zeros((1, 10, 3), np.uint8),
        np.zeros((10, 10, 2), np.uint8),
        np.zeros((10,), np.uint8),
        np.zeros((2, 10, 10, 3), np.uint8),
        np.zeros((10, 10, 3), dtype=object),
        np.zeros((10, 10, 3), dtype=bool),
    ],
)
def test_invalid_frames_rejected(bad) -> None:
    with pytest.raises(ValueError):
        ChangeGate(backend="numpy").process(bad, 0.0)


def test_thread_safety() -> None:
    scene = build_scene("object_appears", seed=4)
    gate = ChangeGate(backend="numpy")
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            for i, f in enumerate(scene.frames):
                r = gate.process(f, float(i))
                assert isinstance(r, GateResult)
        except BaseException as exc:  # pragma: no cover - only on failure
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert not errors
    assert gate.stats()["frames"] == 4 * len(scene.frames)


def test_state_is_bounded() -> None:
    """Long runs keep a fixed amount of state (no per-frame growth)."""
    gate = ChangeGate(backend="numpy")
    frames = static_scene(seed=0, n_frames=3).frames
    for i in range(200):
        gate.process(frames[i % 3], float(i))
    st = gate._state
    assert st.prev is not None and st.background is not None
    assert st.prev.img.shape == st.background.shape == (120, 160)
    assert len(gate._ops._resize_cache) <= 17
